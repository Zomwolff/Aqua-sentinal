"""
Rule-based AIS anomaly detection for the Aqua-Sentinel anomaly-detection service.

Rules:
  1. sudden_stop       — vessel drops speed dramatically outside a port/anchorage
  2. erratic_course    — high circular course variance
  3. speed_anomaly     — speed exceeds vessel-type plausible max
  4. loitering_anomaly — sustained low speed mid-ocean
  5. route_deviation   — tanker/cargo heading away from declared destination
  6. ais_gap           — vessel has not broadcast in too long
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

ERRATIC_COURSE_VARIANCE = float(os.environ.get("ERRATIC_COURSE_VARIANCE", 2500))
LOITERING_THRESHOLD = float(os.environ.get("LOITERING_THRESHOLD", 0.8))
AIS_GAP_THRESHOLD_MIN = int(os.environ.get("AIS_GAP_THRESHOLD_MINUTES", 30))

SUDDEN_STOP_PREV_SPEED_MIN_KN = 5.0
SUDDEN_STOP_CURR_SPEED_MAX_KN = 0.5
COG_HEADING_DIVERGENCE_DEG = 30.0
COG_HEADING_MIN_REPEATS = 3
MAX_RATE_OF_TURN_DEG_MIN = 20.0
TURN_REVERSAL_MIN_COUNT = 3
DRAUGHT_DROP_THRESHOLD_M = 0.5

VESSEL_TYPE_MAX_SPEED: Dict[str, float] = {
    "tanker": 20.0, "cargo": 25.0, "fishing": 15.0, "passenger": 30.0,
    "tug": 12.0, "high_speed": 45.0, "sar": 30.0, "sailing": 15.0,
    "pleasure": 25.0, "wing_in_ground": 80.0, "other": 30.0, "unknown": 35.0,
}

_KNOWN_PORTS: Dict[str, tuple] = {
    "mumbai": (18.936, 72.838), "nhava sheva": (18.948, 72.952),
    "jnpt": (18.948, 72.952), "kandla": (23.003, 70.215),
    "mundra": (22.769, 69.710), "goa": (15.412, 73.798),
    "mormugao": (15.412, 73.798), "mangalore": (12.921, 74.816),
    "cochin": (9.964, 76.279), "kochi": (9.964, 76.279),
    "chennai": (13.104, 80.293), "tuticorin": (8.750, 78.183),
    "visakhapatnam": (17.686, 83.291),
}


def _lookup_port_bearing(lat: float, lon: float, destination: Optional[str]) -> Optional[float]:
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
    Returns list of anomaly event dicts (empty = no anomalies).
    """
    events: List[Dict[str, Any]] = []
    mmsi = features.get("mmsi")
    window_start = features.get("window_start")
    vessel_type = (vessel_meta.get("vessel_type") or "unknown").lower()

    avg_speed = features.get("avg_speed")
    course_variance = features.get("course_variance")
    loitering_score = features.get("loitering_score")
    ping_count = features.get("ping_count", 0)
    mean_divergence = features.get("mean_cog_heading_divergence_deg")
    max_divergence = features.get("max_cog_heading_divergence_deg")
    divergence_repeats = features.get("repeated_cog_heading_divergences", 0)
    max_rate_of_turn = features.get("max_rate_of_turn_deg_min")
    turn_reversals = features.get("turn_reversal_count", 0)
    draught_change = features.get("draught_change_m")

    # Extreme weather flags
    extreme_weather = False
    if weather_meta:
        w_speed = weather_meta.get("wind_speed_kmh")
        if w_speed is not None and w_speed >= 50.0:  # > 50 km/h is rough
            extreme_weather = True
        c_speed = weather_meta.get("current_speed_ms")
        if c_speed is not None and c_speed >= 1.5:  # > 1.5 m/s is very strong current
            extreme_weather = True

    # Rule 1: Sudden Stop
    if (not extreme_weather and avg_speed is not None and avg_speed < SUDDEN_STOP_CURR_SPEED_MAX_KN
            and not port_nearby and previous_features is not None):
        prev_speed = previous_features.get("avg_speed")
        if prev_speed is not None and prev_speed > SUDDEN_STOP_PREV_SPEED_MIN_KN:
            severity = "HIGH" if prev_speed > 10.0 else "MEDIUM"
            events.append({
                "mmsi": mmsi, "window_start": window_start,
                "anomaly_type": "sudden_stop", "severity": severity, "source": "rules",
                "evidence": {
                    "prev_avg_speed_kn": round(prev_speed, 2),
                    "curr_avg_speed_kn": round(avg_speed, 2),
                    "port_nearby": port_nearby,
                    "rule": "speed_dropped_outside_port",
                },
            })

    # Rule 2: Erratic Course
    if not extreme_weather and course_variance is not None and course_variance > ERRATIC_COURSE_VARIANCE:
        severity = "HIGH" if course_variance > ERRATIC_COURSE_VARIANCE * 2 else "MEDIUM"
        events.append({
            "mmsi": mmsi, "window_start": window_start,
            "anomaly_type": "erratic_course", "severity": severity, "source": "rules",
            "evidence": {
                "course_variance": round(course_variance, 2),
                "threshold": ERRATIC_COURSE_VARIANCE,
                "rule": "circular_course_variance_exceeds_threshold",
            },
        })

    # Rule 3: Speed Anomaly
    if avg_speed is not None:
        type_max = VESSEL_TYPE_MAX_SPEED.get(vessel_type, VESSEL_TYPE_MAX_SPEED["unknown"])
        if avg_speed > type_max * 1.5:
            events.append({
                "mmsi": mmsi, "window_start": window_start,
                "anomaly_type": "speed_anomaly", "severity": "HIGH", "source": "rules",
                "evidence": {
                    "avg_speed_kn": round(avg_speed, 2), "vessel_type": vessel_type,
                    "type_max_kn": type_max, "ratio": round(avg_speed / type_max, 2),
                    "rule": "speed_exceeds_1.5x_type_max",
                },
            })
        elif avg_speed > type_max * 1.2:
            events.append({
                "mmsi": mmsi, "window_start": window_start,
                "anomaly_type": "speed_anomaly", "severity": "MEDIUM", "source": "rules",
                "evidence": {
                    "avg_speed_kn": round(avg_speed, 2), "vessel_type": vessel_type,
                    "type_max_kn": type_max, "ratio": round(avg_speed / type_max, 2),
                    "rule": "speed_exceeds_1.2x_type_max",
                },
            })

    # Rule 4: Loitering Anomaly
    if (loitering_score is not None and loitering_score >= LOITERING_THRESHOLD
            and not port_nearby and ping_count >= 3):
        severity = "LOW" if vessel_type == "fishing" else ("HIGH" if vessel_type in ("tanker", "cargo") else "MEDIUM")
        events.append({
            "mmsi": mmsi, "window_start": window_start,
            "anomaly_type": "loitering_anomaly", "severity": severity, "source": "rules",
            "evidence": {
                "loitering_score": round(loitering_score, 4),
                "threshold": LOITERING_THRESHOLD, "vessel_type": vessel_type,
                "port_nearby": port_nearby, "rule": "sustained_low_speed_outside_port",
            },
        })

    # Rule 5: Repeated COG-versus-heading divergence.  A one-off divergence
    # can be current/wind; repetition across a window is the useful signal.
    if (mean_divergence is not None and max_divergence is not None
            and divergence_repeats >= COG_HEADING_MIN_REPEATS
            and mean_divergence >= COG_HEADING_DIVERGENCE_DEG):
        events.append({
            "mmsi": mmsi, "window_start": window_start,
            "anomaly_type": "cog_heading_divergence", "severity": "MEDIUM", "source": "rules",
            "confidence_score": min(0.9, 0.55 + 0.04 * divergence_repeats),
            "evidence": {
                "mean_divergence_deg": round(mean_divergence, 2),
                "max_divergence_deg": round(max_divergence, 2),
                "repeated_observations": divergence_repeats,
                "threshold_deg": COG_HEADING_DIVERGENCE_DEG,
                "rule": "repeated_cog_heading_divergence",
            },
        })

    # Rule 6: High-rate turns and repeated left/right reversals capture
    # erratic zig-zagging even when circular course variance is modest.
    if ((max_rate_of_turn is not None and max_rate_of_turn >= MAX_RATE_OF_TURN_DEG_MIN)
            or turn_reversals >= TURN_REVERSAL_MIN_COUNT):
        events.append({
            "mmsi": mmsi, "window_start": window_start,
            "anomaly_type": "erratic_turn", "severity": "HIGH" if turn_reversals >= 5 else "MEDIUM",
            "source": "rules",
            "confidence_score": min(0.9, 0.55 + 0.06 * turn_reversals),
            "evidence": {
                "max_rate_of_turn_deg_min": max_rate_of_turn,
                "turn_reversal_count": turn_reversals,
                "rate_threshold_deg_min": MAX_RATE_OF_TURN_DEG_MIN,
                "rule": "excessive_turn_rate_or_repeated_reversal",
            },
        })

    # Rule 7: Draught changes are only evaluated when the source delivered
    # multiple actual observations.  A missing/static value is never inferred.
    if draught_change is not None and draught_change <= -DRAUGHT_DROP_THRESHOLD_M:
        events.append({
            "mmsi": mmsi, "window_start": window_start,
            "anomaly_type": "draught_drop", "severity": "HIGH", "source": "rules",
            "confidence_score": min(0.95, 0.6 + abs(draught_change) * 0.2),
            "evidence": {
                "draught_change_m": round(draught_change, 2),
                "threshold_m": DRAUGHT_DROP_THRESHOLD_M,
                "rule": "observed_draught_reduction",
            },
        })

    # Rule 8: Route Deviation
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
                        events.append({
                            "mmsi": mmsi, "window_start": window_start,
                            "anomaly_type": "route_deviation", "severity": "MEDIUM", "source": "rules",
                            "evidence": {
                                "destination": destination,
                                "expected_bearing_deg": round(expected_bearing, 1),
                                "actual_course_deg": round(float(actual_course), 1),
                                "deviation_deg": round(diff, 1),
                                "rule": "course_deviates_>45deg_from_destination",
                            },
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
    """Return an ais_gap anomaly event if vessel has been silent too long, else None.
    Corroborates by ensuring the vessel didn't simply leave the bounding box.
    """
    now = datetime.now(timezone.utc)
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    gap_minutes = (now - last_seen).total_seconds() / 60.0
    if gap_minutes < gap_threshold_minutes:
        return None
        
    lat = vessel_meta.get("last_lat")
    lon = vessel_meta.get("last_lon")
    if lat is not None and lon is not None:
        # Mumbai offshore BBox: 14.0-25.0 N, 68.0-77.5 E.
        # If within 0.1 deg of edge, it probably just left the tracked area.
        if lat < 14.1 or lat > 24.9 or lon < 68.1 or lon > 77.4:
            return None  # Left the tracked area, not a true blackout

    severity = "HIGH" if gap_minutes > 120 else "MEDIUM"
    return {
        "mmsi": mmsi, "window_start": now.isoformat(),
        "anomaly_type": "ais_gap", "severity": severity, "source": "rules",
        "evidence": {
            "last_seen": last_seen.isoformat(), "gap_minutes": round(gap_minutes, 1),
            "gap_threshold_minutes": gap_threshold_minutes, "vessel_type": vessel_meta.get("vessel_type", "unknown"),
            "rule": "no_ais_broadcast_exceeds_threshold",
            "last_lat": lat, "last_lon": lon,
        },
    }
