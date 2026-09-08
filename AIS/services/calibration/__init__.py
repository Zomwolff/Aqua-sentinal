"""
AIS Scoring Calibration Framework

Provides empirically-derived thresholds and model parameters for anomaly detection,
trust scoring, and risk assessment, replacing arbitrary magic numbers with
data-driven configurations.

Public API:
  - get_calibration_config() — Get global calibration config
  - reload_calibration_config() — Force reload (for testing)
  - CalibrationConfig — Main config class
"""

from calibration.config_loader import (
    CalibrationConfig,
    CalibrationConfigError,
    get_calibration_config,
    reload_calibration_config,
)

__all__ = [
    "CalibrationConfig",
    "CalibrationConfigError",
    "get_calibration_config",
    "reload_calibration_config",
]
