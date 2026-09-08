"""
Calibration config loader and validator.

Loads calibration_config.json at service startup and validates that all
required thresholds and parameters are present and reasonable.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


class CalibrationConfigError(Exception):
    """Raised when calibration config is invalid."""
    pass


class CalibrationConfig:
    """
    Calibration configuration manager.
    Loads, validates, and provides access to empirically-derived thresholds.
    """
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize calibration config.
        
        Args:
            config_path: Path to calibration_config.json. If None, looks in:
                1. CALIBRATION_CONFIG_PATH env var
                2. ./calibration_config.json (current dir)
                3. ../calibration/calibration_config.json (relative to this file)
        """
        if config_path is None:
            config_path = os.environ.get(
                "CALIBRATION_CONFIG_PATH",
                "./calibration_config.json"
            )
        
        self.config_path = Path(config_path)
        if not self.config_path.exists():
            # Try relative to this file
            self.config_path = Path(__file__).parent / "calibration_config.json"
        
        if not self.config_path.exists():
            raise CalibrationConfigError(
                f"Calibration config not found at {config_path}. "
                f"Set CALIBRATION_CONFIG_PATH env var or provide path."
            )
        
        log.info(f"Loading calibration config from {self.config_path}")
        with open(self.config_path) as f:
            self.config = json.load(f)
        
        self._validate()
        log.info(f"Calibration config loaded: version {self.config.get('version')}")
    
    def _validate(self) -> None:
        """Validate that config has required sections."""
        required_sections = ["version", "thresholds", "bayesian_parameters"]
        for section in required_sections:
            if section not in self.config:
                raise CalibrationConfigError(f"Missing required section: {section}")
    
    # ─── Threshold Access ───────────────────────────────────────────────────
    
    def get_threshold(self, name: str, default: Optional[float] = None) -> float:
        """
        Get a threshold value by name.
        
        Args:
            name: Threshold name (e.g., "sudden_stop_speed_threshold_kn")
            default: Default value if not found
        
        Returns:
            Threshold value
        
        Raises:
            CalibrationConfigError: If threshold not found and no default
        """
        thresholds = self.config.get("thresholds", {})
        if name not in thresholds:
            if default is not None:
                log.warning(
                    f"Threshold '{name}' not found in config, using default {default}"
                )
                return default
            raise CalibrationConfigError(f"Threshold not found: {name}")
        
        value = thresholds[name].get("value")
        if value is None:
            raise CalibrationConfigError(f"Invalid threshold '{name}': no 'value' field")
        
        return float(value)
    
    def get_threshold_dict(self, name: str) -> Dict[str, Any]:
        """Get full threshold dict including confidence interval and reasoning."""
        thresholds = self.config.get("thresholds", {})
        if name not in thresholds:
            raise CalibrationConfigError(f"Threshold not found: {name}")
        return thresholds[name]
    
    def get_threshold_ci(self, name: str) -> tuple[float, float]:
        """
        Get confidence interval [lower, upper] for a threshold.
        
        Returns:
            (lower_ci, upper_ci) tuple
        """
        threshold = self.get_threshold_dict(name)
        ci = threshold.get("confidence_interval")
        if ci is None or len(ci) < 2:
            log.warning(f"No CI available for {name}")
            return (threshold["value"], threshold["value"])
        return (float(ci[0]), float(ci[1]))
    
    # ─── Bayesian Parameters ────────────────────────────────────────────────
    
    def get_prior_threat_probability(self) -> float:
        """Get P(threat) = base rate of actual threats in population."""
        bayesian = self.config.get("bayesian_parameters", {})
        prior = bayesian.get("prior_probability_threat", {})
        return float(prior.get("value", 0.10))  # default 10%
    
    def get_gps_error_meters(self) -> float:
        """Get estimated GPS error for position-based thresholds."""
        bayesian = self.config.get("bayesian_parameters", {})
        gps = bayesian.get("gps_error_estimate_meters", {})
        return float(gps.get("value", 100.0))  # default ±100m
    
    def get_ais_lag_seconds(self) -> float:
        """Get estimated AIS reporting lag."""
        bayesian = self.config.get("bayesian_parameters", {})
        lag = bayesian.get("ais_reporting_lag_seconds", {})
        return float(lag.get("value", 10.0))  # default 10s
    
    def get_vessel_max_speed(self, vessel_type: str = "unknown") -> float:
        """
        Get maximum plausible speed for vessel type.
        
        Args:
            vessel_type: Vessel type (tanker, cargo, fishing, etc)
        
        Returns:
            Max speed in knots
        """
        bayesian = self.config.get("bayesian_parameters", {})
        speeds = bayesian.get("vessel_speed_max_by_type", {})
        vtype = vessel_type.lower()
        
        if vtype in speeds:
            return float(speeds[vtype].get("value", 25.0))
        return float(speeds.get("other", {}).get("value", 25.0))
    
    def get_weather_discount(self, wind_speed_kmh: float) -> float:
        """
        Get weather discount factor for anomaly scoring.
        
        Args:
            wind_speed_kmh: Wind speed in km/h
        
        Returns:
            Discount factor [0, 1] to apply to anomaly score
        """
        bayesian = self.config.get("bayesian_parameters", {})
        
        # Severe weather threshold
        severe_threshold = float(
            bayesian.get("weather_wind_threshold_severe_kmh", {}).get("value", 40)
        )
        severe_factor = float(
            bayesian.get("weather_wind_threshold_severe_kmh", {}).get(
                "weather_discount_factor", 0.4
            )
        )
        
        # Rough weather threshold
        rough_threshold = float(
            bayesian.get("weather_wind_threshold_rough_kmh", {}).get("value", 25)
        )
        rough_factor = float(
            bayesian.get("weather_wind_threshold_rough_kmh", {}).get(
                "weather_discount_factor", 0.7
            )
        )
        
        if wind_speed_kmh > severe_threshold:
            return severe_factor
        elif wind_speed_kmh > rough_threshold:
            return rough_factor
        return 1.0  # no discount for normal weather
    
    # ─── Risk Scoring ──────────────────────────────────────────────────────
    
    def get_risk_tier_threshold(self, tier: str) -> float:
        """
        Get posterior probability threshold for risk tier.
        
        Args:
            tier: Risk tier (low, medium, high, critical)
        
        Returns:
            Posterior probability threshold
        """
        risk = self.config.get("risk_scoring", {})
        bayesian = risk.get("bayesian_weights_v2", {})
        mapping = bayesian.get("posterior_to_tier_mapping", {})
        
        # Map tier name to max/min threshold keys
        thresholds = {
            "low": mapping.get("low_risk_max", 0.20),
            "medium": mapping.get("medium_risk_min", 0.20),
            "high": mapping.get("high_risk_min", 0.50),
            "critical": mapping.get("critical_risk_min", 0.75),
        }
        
        return float(thresholds.get(tier.lower(), 0.5))
    
    # ─── Config Metadata ──────────────────────────────────────────────────
    
    def get_version(self) -> str:
        """Get calibration config version."""
        return str(self.config.get("version", "unknown"))
    
    def get_generated_at(self) -> str:
        """Get when config was generated."""
        return str(self.config.get("generated_at", "unknown"))
    
    def get_source(self) -> str:
        """Get source/description of config."""
        return str(self.config.get("source", "unknown"))
    
    def to_dict(self) -> Dict[str, Any]:
        """Get full config dict."""
        return self.config
    
    def __repr__(self) -> str:
        return (
            f"CalibrationConfig("
            f"version={self.get_version()}, "
            f"path={self.config_path})"
        )


# Global singleton instance
_instance: Optional[CalibrationConfig] = None


def get_calibration_config(config_path: Optional[str] = None) -> CalibrationConfig:
    """
    Get or create global calibration config singleton.
    
    Args:
        config_path: Path to config file (only used on first call)
    
    Returns:
        CalibrationConfig instance
    """
    global _instance
    if _instance is None:
        _instance = CalibrationConfig(config_path)
    return _instance


def reload_calibration_config(config_path: Optional[str] = None) -> CalibrationConfig:
    """
    Force reload of calibration config (useful for testing/updates).
    
    Args:
        config_path: Path to config file
    
    Returns:
        New CalibrationConfig instance
    """
    global _instance
    _instance = CalibrationConfig(config_path)
    return _instance
