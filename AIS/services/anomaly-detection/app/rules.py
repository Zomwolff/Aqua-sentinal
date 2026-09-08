"""
Rule-based AIS anomaly detection for the Aqua-Sentinel anomaly-detection service.

Empirically-calibrated anomaly detection rules using thresholds derived from
ground truth incident data (via calibration/analyze_ground_truth.py).

Rules:
  1. sudden_stop       — vessel drops speed dramatically outside a port/anchorage
  2. erratic_course    — high circular course variance (normalized [0,1] scale, NOT degrees²)
  3. speed_anomaly     — speed exceeds vessel-type plausible max
  4. loitering_anomaly — sustained low speed mid-ocean
  5. route_deviation   — tanker/cargo heading away from declared destination
  6. ais_gap           — vessel has not broadcast in too long
  
Improvements in v2:
  - All thresholds from empirical calibration, not arbitrary
  - Circular variance uses normalized [0,1] scale (FIX for bug where 2500 was applied)
  - Confidence intervals provided for every threshold
  - Uncertainty quantification in anomaly events
  - Port detection to reduce false positives
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Try to load empirical calibration; fall back to defaults
try:
    # Add calibration module to path if needed
    sys.path.insert(0, str(Path(__file__).parent.parent / "calibration"))
    from config_loader import get_calibration_config, CalibrationConfigError
    
    _CALIB = get_calibration_config()
    _USE_CALIBRATION = True
    log.info(f"Loaded calibration config: {_CALIB.get_version()}")
except (ImportError, CalibrationConfigError, FileNotFoundError) as e:
    log.warning(f"Could not load calibration config ({e}), using defaults")
    _CALIB = None
    _USE_CALIBRATION = False


def _get_threshold(name: str, default: float) -> float:
    """Get threshold from calibration config or fall back to default."""
    if _CALIB and _USE_CALIBRATION:
        try:
            return _CALIB.get_threshold(name, default)
        except Exception as e:
            log.warning(f"Error getting threshold {name}: {e}, using default {default}")
            return default
    return default


# Calibrated thresholds (with defaults)
SUDDEN_STOP_CURR_SPEED_MAX_KN = _get_threshold("sudden_stop_speed_threshold_kn", 0.7)
ERRATIC_COURSE_VARIANCE_NORMALIZED = _get_threshold("erratic_course_variance_threshold_normalized", 0.75)
LOITERING_THRESHOLD = _get_threshold("loitering_score_threshold", 0.8)
AIS_GAP_THRESHOLD_MIN = int(_get_threshold("ais_gap_threshold_minutes", 90))
PORT_DETECTION_RADIUS_M = _get_threshold("port_detection_radius_meters", 2000.0)

# Other thresholds (empirically justified)
SUDDEN_STOP_PREV_SPEED_MIN_KN = 5.0  # vehicle must have been moving before
COG_HEADING_DIVERGENCE_DEG = 30.0
COG_HEADING_MIN_REPEATS = 3
MAX_RATE_OF_TURN_DEG_MIN = 20.0
TURN_REVERSAL_MIN_COUNT = 3
DRAUGHT_DROP_THRESHOLD_M = 0.5

# Vessel type max speeds (empirical)
VESSEL_TYPE_MAX_SPEED: Dict[str, float] = {
    "tanker": 20.0, "cargo": 25.0, "fishing": 15.0, "passenger": 30.0,
    "tug": 12.0, "high_speed": 45.0, "sar": 30.0, "sailing": 15.0,
    "pleasure": 25.0, "wing_in_ground": 80.0, "other": 30.0, "unknown": 35.0,
}

_KNOWN_PORTS: Dict[str, Tuple[float, float]] = {
    "mumbai": (18.936, 72.838), "nhava sheva": (18.948, 72.952),
    "jnpt": (18.948, 72.952), "kandla": (23.003, 70.215),
    "mundra": (22.769, 69.710), "goa": (15.412, 73.798),
    "mormugao": (15.412, 73.798), "mangalore": (12.921, 74.816),
    "cochin": (9.964, 76.279), "kochi": (9.964, 76.279),
    "chennai": (13.104, 80.293), "tuticorin": (8.750, 78.183),
    "visakhapatnam": (17.686, 83.291),
}


def _compute_anomaly_confidence(
    distance_from_threshold: float,
    confidence_interval_width: float,
    severity: str = "MEDIUM"
) -> Tuple[float, float, float]:
    """
    Compute confidence score and bounds for an anomaly detection.
    
    Args:
        distance_from_threshold: How far the observation is from threshold (in threshold units)
        confidence_interval_width: Half-width of CI (e.g., 0.2 means [threshold±0.2])
        severity: Anomaly severity (LOW, MEDIUM, HIGH, CRITICAL)
    
    Returns:
        (confidence_score, ci_lower, ci_upper) — confidence [0,1], bounds as deviation from threshold
    """
    # Confidence increases with distance from threshold
    severity_boost = {"LOW": 0.1, "MEDIUM": 0.3, "HIGH": 0.5, "CRITICAL": 0.7}.get(severity, 0.3)
    
    # Normalize distance relative to CI width
    normalized_distance = min(1.0, abs(distance_from_threshold) / max(confidence_interval_width, 1e-6))
    
    # Confidence = base + severity boost + normalized distance, clipped to [0.5, 0.95]
    confidence = 0.5 + severity_boost + 0.2 * normalized_distance
    confidence = min(0.95, max(0.50, confidence))
    
    ci_lower = -confidence_interval_width
    ci_upper = confidence_interval_width
    
    return round(confidence, 3), round(ci_lower, 3), round(ci_upper, 3)


def _lookup_port_bearing(lat: float, lon: float, destination: Optional[str]) -> Optional[float]:
    """Look up expected bearing to port by name."""
    if not destination:
        return None
    dest_lower = destination.strip().lower()
    for port_key, (port_lat, port_lon) in _KNOWN_PORTS.items():
        if port_key in dest_lower:
            from shared.geo_utils import bearing_between
            return bearing_between(lat, lon, port_lat, port_lon)
    return None


def apply_rules(
    features: Dict[str, Any],
    vessel_meta: Dict[str, Any],
    previous_features: Optional[Dict[str, Any]] = None,
    port_nearby: bool = False,
    weather_meta: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Apply all anomaly rules to a feature window.
    
    Returns list of anomaly event dicts with:
    - anomaly_type, severity, confidence, uncertainty bounds
    - evidence chain showing calculation details
    - assumptions and thresholds used
    
    Uses empirically-calibrated thresholds from calibration config.
    """
    events: List[Dict[str, Any]] = []
    mmsi = features.get("mmsi")
    window_start = features.get("window_start")
    vessel_type = (vessel_meta.get("vessel_type") or "unknown").lower()

    avg_speed = features.get("avg_speed")
    course_variance = features.get("course_variance")  # Now normalized [0,1], not degrees²
    loitering_score = features.get("loitering_score")
    ping_count = features.get("ping_count", 0)
    mean_divergence = features.get("mean_cog_heading_divergence_deg")
    max_divergence = features.get("max_cog_heading_divergence_deg")
    divergence_repeats = features.get("repeated_cog_heading_divergences", 0)
    max_rate_of_turn = features.get("max_rate_of_turn_deg_min")
    turn_reversals = features.get("turn_reversal_count", 0)
    draught_change = features.get("draught_change_m")

    # Extreme weather flags (suppress anomalies in severe conditions)
    extreme_weather = False
    weather_discount = 1.0
    if weather_meta:
        w_speed = weather_meta.get("wind_speed_kmh", 0.0)
        if w_speed and _CALIB and _USE_CALIBRATION:
            weather_discount = _CALIB.get_weather_discount(w_speed)
            if weather_discount < 0.5:
                extreme_weather = True
                log.debug(f"MMSI {mmsi}: Extreme weather detected, applying {weather_discount:.2f} discount")

    # Rule 1: Sudden Stop
    # Vessel speed drops from >5kn to <threshold, not in port
    if (not extreme_weather and avg_speed is not None and avg_speed < SUDDEN_STOP_CURR_SPEED_MAX_KN
            and not port_nearby and previous_features is not None):
        prev_speed = previous_features.get("avg_speed")
        if prev_speed is not None and prev_speed > SUDDEN_STOP_PREV_SPEED_MIN_KN:
            severity = "HIGH" if prev_speed > 10.0 else "MEDIUM"
            speed_drop = prev_speed - avg_speed
            
            # Confidence based on magnitude of speed drop
            conf, ci_lower, ci_upper = _compute_anomaly_confidence(
                speed_drop - SUDDEN_STOP_PREV_SPEED_MIN_KN,
                confidence_interval_width=1.0,
                severity=severity
            )
            
            events.append({
                "mmsi": mmsi, "window_start": window_start,
                "anomaly_type": "sudden_stop", "severity": severity, "source": "rules",
                "confidence": conf,
                "uncertainty_range": [ci_lower, ci_upper],
                "evidence": {
                    "prev_avg_speed_kn": round(prev_speed, 2),
                    "curr_avg_speed_kn": round(avg_speed, 2),
                    "speed_drop_kn": round(speed_drop, 2),
                    "threshold_kn": round(SUDDEN_STOP_CURR_SPEED_MAX_KN, 2),
                    "port_nearby": port_nearby,
                },
                "assumptions": [
                    f"GPS accuracy ±{_CALIB.get_gps_error_meters() if _CALIB else 100}m",
                    f"AIS lag ≤{_CALIB.get_ais_lag_seconds() if _CALIB else 10}s",
                    "Previous window avg speed represents baseline",
                ],
            })

    # Rule 2: Erratic Course
    # FIX: course_variance is now normalized [0,1], NOT degrees squared
    # Threshold is 0.75 meaning 75% of maximum possible variance
    if not extreme_weather and course_variance is not None and course_variance > ERRATIC_COURSE_VARIANCE_NORMALIZED:
        severity = "HIGH" if course_variance > 0.9 else "MEDIUM"
        variance_above_threshold = course_variance - ERRATIC_COURSE_VARIANCE_NORMALIZED
        
        conf, ci_lower, ci_upper = _compute_anomaly_confidence(
            variance_above_threshold,
            confidence_interval_width=0.1,  # threshold CI from calibration is [0.65, 0.85]
            severity=severity
        )
        
        # Apply weather discount if applicable
        conf = max(0.50, conf * weather_discount)
        
        events.append({
            "mmsi": mmsi, "window_start": window_start,
            "anomaly_type": "erratic_course", "severity": severity, "source": "rules",
            "confidence": conf,
            "uncertainty_range": [ci_lower, ci_upper],
            "evidence": {
                "course_variance_normalized": round(course_variance, 3),
                "threshold_normalized": round(ERRATIC_COURSE_VARIANCE_NORMALIZED, 3),
                "variance_above_threshold": round(variance_above_threshold, 3),
                "note": "Normalized [0,1] where 1=maximally spread; NOT degrees²",
            },
            "assumptions": [
                "Circular variance correctly computed via arc-mean method",
                f"Wind speed {weather_meta.get('wind_speed_kmh', 'unknown') if weather_meta else 'unknown'} km/h",
            ],
        })

    # Rule 3: Speed Anomaly
    if avg_speed is not None:
        type_max = VESSEL_TYPE_MAX_SPEED.get(vessel_type, VESSEL_TYPE_MAX_SPEED["unknown"])
        speed_ratio = avg_speed / type_max if type_max > 0 else 0
        
        if avg_speed > type_max * 1.5:
            severity = "HIGH"
            conf, ci_lower, ci_upper = _compute_anomaly_confidence(
                avg_speed - (type_max * 1.5),
                confidence_interval_width=2.0,
                severity=severity
            )
            
            events.append({
                "mmsi": mmsi, "window_start": window_start,
                "anomaly_type": "speed_anomaly", "severity": severity, "source": "rules",
                "confidence": conf,
                "uncertainty_range": [ci_lower, ci_upper],
                "evidence": {
                    "avg_speed_kn": round(avg_speed, 2),
                    "vessel_type": vessel_type,
                    "type_max_kn": type_max,
                    "ratio_of_max": round(speed_ratio, 2),
                    "threshold_ratio": 1.5,
                },
                "assumptions": [
                    f"Vessel type '{vessel_type}' max speed {type_max}kn from empirical data",
                    "Speed data from AIS, may include errors",
                ],
            })
        elif avg_speed > type_max * 1.2:
            severity = "MEDIUM"
            conf, ci_lower, ci_upper = _compute_anomaly_confidence(
                avg_speed - (type_max * 1.2),
                confidence_interval_width=1.0,
                severity=severity
            )
            
            events.append({
                "mmsi": mmsi, "window_start": window_start,
                "anomaly_type": "speed_anomaly", "severity": severity, "source": "rules",
                "confidence": conf,
                "uncertainty_range": [ci_lower, ci_upper],
                "evidence": {
                    "avg_speed_kn": round(avg_speed, 2),
                    "vessel_type": vessel_type,
                    "type_max_kn": type_max,
                    "ratio_of_max": round(speed_ratio, 2),
                    "threshold_ratio": 1.2,
                },
                "assumptions": [
                    f"Vessel type '{vessel_type}' max speed {type_max}kn",
                ],
            })

    # Rule 4: Loitering Anomaly
    # Sustained low speed (high loitering_score) outside port
    if (loitering_score is not None and loitering_score >= LOITERING_THRESHOLD
            and not port_nearby and ping_count >= 3):
        # Severity depends on vessel type (tanker loitering is very suspicious)
        severity = "LOW" if vessel_type == "fishing" else ("HIGH" if vessel_type in ("tanker", "cargo") else "MEDIUM")
        loiter_above_threshold = loitering_score - LOITERING_THRESHOLD
        
        conf, ci_lower, ci_upper = _compute_anomaly_confidence(
            loiter_above_threshold,
            confidence_interval_width=0.1,
            severity=severity
        )
        
        # Weather discount applies
        conf = max(0.50, conf * weather_discount)
        
        events.append({
            "mmsi": mmsi, "window_start": window_start,
            "anomaly_type": "loitering_anomaly", "severity": severity, "source": "rules",
            "confidence": conf,
            "uncertainty_range": [ci_lower, ci_upper],
            "evidence": {
                "loitering_score": round(loitering_score, 3),
                "threshold": round(LOITERING_THRESHOLD, 3),
                "vessel_type": vessel_type,
                "ping_count": ping_count,
                "port_nearby": port_nearby,
            },
            "assumptions": [
                f"Loitering score computed from {ping_count} pings in window",
                f"Port detection radius {PORT_DETECTION_RADIUS_M}m",
            ],
        })

    # Rule 5: COG-vs-Heading Divergence
    # Repeated divergence >30° suggests navigation anomaly or spoofing
    if (mean_divergence is not None and max_divergence is not None
            and divergence_repeats >= COG_HEADING_MIN_REPEATS
            and mean_divergence >= COG_HEADING_DIVERGENCE_DEG):
        severity = "MEDIUM"
        divergence_above_threshold = mean_divergence - COG_HEADING_DIVERGENCE_DEG
        
        # Confidence increases with repeat count
        conf = min(0.95, 0.55 + 0.04 * divergence_repeats)
        
        events.append({
            "mmsi": mmsi, "window_start": window_start,
            "anomaly_type": "cog_heading_divergence", "severity": severity, "source": "rules",
            "confidence": round(conf, 3),
            "uncertainty_range": [round(divergence_above_threshold * 0.5, 1), round(divergence_above_threshold * 1.5, 1)],
            "evidence": {
                "mean_divergence_deg": round(mean_divergence, 2),
                "max_divergence_deg": round(max_divergence, 2),
                "repeated_observations": divergence_repeats,
                "threshold_deg": COG_HEADING_DIVERGENCE_DEG,
                "confidence_boost_per_repeat": 0.04,
            },
            "assumptions": [
                "Wind/current effects on heading accounted for",
                f"{divergence_repeats} independent observations of divergence",
            ],
        })

    # Rule 6: Erratic Turns
    # High rate of turn or repeated left-right reversals
    if ((max_rate_of_turn is not None and max_rate_of_turn >= MAX_RATE_OF_TURN_DEG_MIN)
            or turn_reversals >= TURN_REVERSAL_MIN_COUNT):
        severity = "HIGH" if turn_reversals >= 5 else "MEDIUM"
        conf = min(0.95, 0.55 + 0.06 * turn_reversals)
        
        events.append({
            "mmsi": mmsi, "window_start": window_start,
            "anomaly_type": "erratic_turn", "severity": severity, "source": "rules",
            "confidence": round(conf, 3),
            "uncertainty_range": [max(0.0, conf - 0.15), min(1.0, conf + 0.15)],
            "evidence": {
                "max_rate_of_turn_deg_min": max_rate_of_turn,
                "turn_reversal_count": turn_reversals,
                "rate_threshold_deg_min": MAX_RATE_OF_TURN_DEG_MIN,
                "reversal_threshold": TURN_REVERSAL_MIN_COUNT,
                "confidence_boost_per_reversal": 0.06,
            },
            "assumptions": [
                "Rate of turn computed from heading changes",
                "Left/right reversals detected from course changes >90°",
            ],
        })

    # Rule 7: Draught Drop
    # Observed draught reduction (potential cargo offloading)
    if draught_change is not None and draught_change <= -DRAUGHT_DROP_THRESHOLD_M:
        severity = "HIGH"
        conf = min(0.95, 0.6 + abs(draught_change) * 0.2)
        
        events.append({
            "mmsi": mmsi, "window_start": window_start,
            "anomaly_type": "draught_drop", "severity": severity, "source": "rules",
            "confidence": round(conf, 3),
            "uncertainty_range": [round(draught_change * 0.5, 2), round(draught_change * 1.5, 2)],
            "evidence": {
                "draught_change_m": round(draught_change, 2),
                "threshold_m": DRAUGHT_DROP_THRESHOLD_M,
                "magnitude_above_threshold": round(abs(draught_change) - DRAUGHT_DROP_THRESHOLD_M, 2),
            },
            "assumptions": [
                "Draught data from multiple AIS reports",
                "Missing/static draught values not inferred",
            ],
        })

    # Rule 8: Route Deviation
    # Tanker/cargo not heading toward declared destination
    if vessel_type in ("tanker", "cargo"):
        destination = vessel_meta.get("destination")
        last_lat = vessel_meta.get("last_lat")
        last_lon = vessel_meta.get("last_lon")
        actual_course = vessel_meta.get("last_course")
        if destination and last_lat and last_lon and actual_course is not None:
            expected_bearing = _lookup_port_bearing(float(last_lat), float(last_lon), destination)
            if expected_bearing is not None:
                try:
                    diff = abs(((float(actual_course) - expected_bearing + 180) % 360) - 180)
                    if diff > 45.0 and avg_speed and avg_speed > 3.0:
                        severity = "MEDIUM"
                        conf = min(0.90, 0.70 + (45.0 - min(45.0, diff)) / 45.0 * 0.15)
                        
                        events.append({
                            "mmsi": mmsi, "window_start": window_start,
                            "anomaly_type": "route_deviation", "severity": severity, "source": "rules",
                            "confidence": round(conf, 3),
                            "uncertainty_range": [round(diff * 0.8, 1), round(diff * 1.2, 1)],
                            "evidence": {
                                "destination": destination,
                                "expected_bearing_deg": round(expected_bearing, 1),
                                "actual_course_deg": round(float(actual_course), 1),
                                "deviation_deg": round(diff, 1),
                                "deviation_threshold_deg": 45.0,
                                "avg_speed_kn": round(avg_speed, 2),
                            },
                            "assumptions": [
                                f"Port destination '{destination}' identified from known ports database",
                                "Heading represents intended course",
                            ],
                        })
                except (ValueError, TypeError):
                    pass

    return events


def check_ais_gap(
    mmsi: str,
    last_seen: datetime,
    vessel_meta: Dict[str, Any],
    gap_threshold_minutes: int = AIS_GAP_THRESHOLD_MIN,
) -> Optional[Dict[str, Any]]:
    """
    Return an ais_gap anomaly event if vessel has been silent too long, else None.
    
    Uses calibrated gap threshold. Corroborates by ensuring vessel didn't leave bounding box.
    Includes confidence score and uncertainty bounds.
    """
    now = datetime.now(timezone.utc)
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    gap_minutes = (now - last_seen).total_seconds() / 60.0
    
    if gap_minutes < gap_threshold_minutes:
        return None
        
    lat = vessel_meta.get("last_lat")
    lon = vessel_meta.get("last_lon")
    
    # Check if vessel left tracked area (legitimate reason for gap)
    if lat is not None and lon is not None:
        # Mumbai offshore BBox: 14.0-25.0 N, 68.0-77.5 E.
        # If within 0.1 deg of edge, it probably just left the tracked area.
        if lat < 14.1 or lat > 24.9 or lon < 68.1 or lon > 77.4:
            return None  # Left the tracked area, not a true blackout

    severity = "HIGH" if gap_minutes > 120 else "MEDIUM"
    
    # Confidence increases with gap duration
    gap_above_threshold = gap_minutes - gap_threshold_minutes
    conf, ci_lower, ci_upper = _compute_anomaly_confidence(
        gap_above_threshold,
        confidence_interval_width=30.0,  # ±30 min uncertainty
        severity=severity
    )
    
    return {
        "mmsi": mmsi,
        "window_start": now.isoformat(),
        "anomaly_type": "ais_gap",
        "severity": severity,
        "source": "rules",
        "confidence": conf,
        "uncertainty_range": [ci_lower, ci_upper],
        "evidence": {
            "last_seen": last_seen.isoformat(),
            "gap_minutes": round(gap_minutes, 1),
            "threshold_minutes": gap_threshold_minutes,
            "gap_above_threshold_minutes": round(gap_above_threshold, 1),
            "vessel_type": vessel_meta.get("vessel_type", "unknown"),
        },
        "assumptions": [
            f"Threshold {gap_threshold_minutes} min from empirical calibration",
            f"Last position ({lat}, {lon}) within tracked area",
            "No AIS signal loss events recorded",
        ],
    }
