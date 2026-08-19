"""
AIS behavioral feature extraction for the Aqua-Sentinel analytics service.

For each vessel window, computes:
  - avg_speed, speed_variance, max_speed
  - course_variance (circular, not naive — handles 0°/360° correctly)
  - heading_change_rate (degrees per minute)
  - loitering_score (port-proximity discounted)
  - distance_traveled_km
  - proximity_events (other MMSIs within PROXIMITY_RADIUS_M)

All edge cases are handled explicitly.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

PROXIMITY_RADIUS_M = float(os.environ.get("PROXIMITY_RADIUS_M", 1000))

# AIS sentinel values that mean "not available" — exclude from calculations
AIS_SPEED_NA = {102.2, 102.3, 1023}
AIS_COURSE_NA = {360.0, 511.0}
AIS_HEADING_NA = {511, 511.0}


def _safe_speeds(pings: List[Dict[str, Any]]) -> List[float]:
    """Extract valid speed values from pings, excluding AIS 'not available' sentinels."""
    speeds = []
    for p in pings:
        s = p.get("speed_knots")
        if s is None:
            continue
        try:
            sf = float(s)
        except (TypeError, ValueError):
            continue
        if sf in AIS_SPEED_NA or sf > 102.0:
            continue
        speeds.append(sf)
    return speeds


def _safe_courses(pings: List[Dict[str, Any]]) -> List[float]:
    """Extract valid course values, excluding AIS 'not available' sentinels."""
    courses = []
    for p in pings:
        c = p.get("course")
        if c is None:
            continue
        try:
            cf = float(c)
        except (TypeError, ValueError):
            continue
        if cf >= 360.0:  # 360 means not available in AIS
            continue
        courses.append(cf)
    return courses


def _safe_headings(pings: List[Dict[str, Any]]) -> List[float]:
    """Extract valid heading values, excluding AIS 'not available' (511)."""
    headings = []
    for p in pings:
        h = p.get("heading")
        if h is None:
            continue
        try:
            hf = float(h)
        except (TypeError, ValueError):
            continue
        if hf == 511.0 or hf > 360.0:
            continue
        headings.append(hf)
    return headings


def _parse_ts(ping: Dict[str, Any]) -> Optional[datetime]:
    """Parse a ping's timestamp field to datetime."""
    ts_raw = ping.get("timestamp") or ping.get("ts_epoch")
    if isinstance(ts_raw, datetime):
        return ts_raw
    if isinstance(ts_raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts_raw))
        except (ValueError, OSError):
            return None
    if isinstance(ts_raw, str):
        try:
            return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return None


def compute_circular_variance(angles_deg: List[float]) -> float:
    """
    Circular variance of angles in degrees.
    Returns a value in [0, 1]: 0 = all identical, 1 = maximally spread.
    Scaled to [0, 360²] ≈ [0, 129600] for threshold comparison with
    the ERRATIC_COURSE_VARIANCE config (default 2500).

    This is the CORRECT implementation. Naive variance breaks at 355°/5°
    boundary because (355 - 5)^2 is huge even though they are only 10° apart.
    """
    if len(angles_deg) < 2:
        return 0.0
    radians = [math.radians(a) for a in angles_deg]
    sin_mean = sum(math.sin(r) for r in radians) / len(radians)
    cos_mean = sum(math.cos(r) for r in radians) / len(radians)
    r_bar = math.sqrt(sin_mean ** 2 + cos_mean ** 2)
    circ_var_01 = 1.0 - r_bar  # in [0, 1]
    # Scale to approximate degree² space for threshold comparison
    return circ_var_01 * (360.0 ** 2)


def compute_heading_change_rate(pings: List[Dict[str, Any]]) -> Optional[float]:
    """
    Mean absolute heading change per minute across consecutive pings.
    Returns None if fewer than 2 pings with valid headings.
    """
    valid_pings = [(p, _parse_ts(p)) for p in pings
                   if p.get("heading") not in (None, 511, 511.0)]
    valid_pings = [(p, ts) for p, ts in valid_pings if ts is not None]
    if len(valid_pings) < 2:
        return None

    deltas = []
    for i in range(1, len(valid_pings)):
        h1 = float(valid_pings[i-1][0]["heading"])
        h2 = float(valid_pings[i][0]["heading"])
        dt_s = (valid_pings[i][1] - valid_pings[i-1][1]).total_seconds()
        if dt_s <= 0:
            continue
        diff = abs(((h2 - h1 + 180) % 360) - 180)  # smallest angle difference
        deltas.append(diff / (dt_s / 60.0))  # degrees per minute

    return sum(deltas) / len(deltas) if deltas else None


def _angular_difference(first: float, second: float) -> float:
    """Smallest absolute separation between two compass angles."""
    return abs(((first - second + 180.0) % 360.0) - 180.0)


def compute_motion_integrity_features(pings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute per-ping changes that averages alone would hide.

    All rate features are time-normalised and use consecutive source timestamps;
    unsorted, duplicate, and unavailable AIS values are ignored rather than
    converted to zero.
    """
    ordered = []
    for ping in pings:
        timestamp = _parse_ts(ping)
        if timestamp is not None:
            ordered.append((timestamp, ping))
    ordered.sort(key=lambda item: item[0])

    speed_deltas: List[float] = []
    turn_rates: List[float] = []
    signed_turns: List[float] = []
    for (before_ts, before), (after_ts, after) in zip(ordered, ordered[1:]):
        elapsed_min = (after_ts - before_ts).total_seconds() / 60.0
        if elapsed_min <= 0:
            continue
        try:
            before_speed, after_speed = float(before.get("speed_knots")), float(after.get("speed_knots"))
            if before_speed not in AIS_SPEED_NA and after_speed not in AIS_SPEED_NA:
                speed_deltas.append(after_speed - before_speed)
        except (TypeError, ValueError):
            pass

        # Heading is preferred for turn rate; course is a valid fallback when
        # a gyro heading is unavailable.
        before_angle = before.get("heading") if before.get("heading") not in (None, 511, 511.0) else before.get("course")
        after_angle = after.get("heading") if after.get("heading") not in (None, 511, 511.0) else after.get("course")
        try:
            before_angle, after_angle = float(before_angle), float(after_angle)
            if 0 <= before_angle < 360 and 0 <= after_angle < 360:
                signed_delta = ((after_angle - before_angle + 180.0) % 360.0) - 180.0
                signed_turns.append(signed_delta)
                turn_rates.append(abs(signed_delta) / elapsed_min)
        except (TypeError, ValueError):
            pass

    divergences = []
    for ping in pings:
        try:
            course, heading = float(ping.get("course")), float(ping.get("heading"))
            if 0 <= course < 360 and 0 <= heading < 360:
                divergences.append(_angular_difference(course, heading))
        except (TypeError, ValueError):
            continue

    reversals = sum(
        1 for previous, current in zip(signed_turns, signed_turns[1:])
        if previous * current < 0 and abs(previous) >= 10 and abs(current) >= 10
    )
    return {
        "max_speed_delta_kn": round(max(speed_deltas), 3) if speed_deltas else None,
        "max_speed_drop_kn": round(abs(min(speed_deltas)), 3) if speed_deltas and min(speed_deltas) < 0 else 0.0,
        "mean_cog_heading_divergence_deg": round(sum(divergences) / len(divergences), 3) if divergences else None,
        "max_cog_heading_divergence_deg": round(max(divergences), 3) if divergences else None,
        "repeated_cog_heading_divergences": sum(1 for value in divergences if value >= 30.0),
        "max_rate_of_turn_deg_min": round(max(turn_rates), 3) if turn_rates else None,
        "turn_reversal_count": reversals,
    }


def compute_loitering_score(
    pings: List[Dict[str, Any]],
    port_nearby: bool = False,
) -> float:
    """
    Loitering score in [0, 1].

    Formula: (fraction of pings with speed < 1.0 knot) * port_discount_factor

    Port discount: if the vessel is within 2km of a known port/anchorage,
    loitering is normal behaviour (it's likely at berth). The discount
    factor (0.2) reduces the score so these vessels don't false-positive.

    A fishing vessel stationary mid-ocean will score 1.0.
    The same vessel at berth in Mumbai Port will score 0.2.
    """
    speeds = _safe_speeds(pings)
    if not speeds:
        return 0.0
    low_speed_count = sum(1 for s in speeds if s < 1.0)
    raw_score = low_speed_count / len(speeds)
    port_discount_factor = 0.2 if port_nearby else 1.0
    return round(raw_score * port_discount_factor, 4)


def compute_distance_traveled_km(pings: List[Dict[str, Any]]) -> float:
    """
    Total great-circle distance traveled across consecutive pings, in km.
    Only counts pings with valid lat/lon.
    """
    from shared.geo_utils import haversine_distance
    valid = [(float(p["lat"]), float(p["lon"]))
             for p in pings if p.get("lat") is not None and p.get("lon") is not None]
    if len(valid) < 2:
        return 0.0
    total_m = sum(
        haversine_distance(valid[i-1][0], valid[i-1][1], valid[i][0], valid[i][1])
        for i in range(1, len(valid))
    )
    return round(total_m / 1000.0, 4)


def compute_proximity_events(
    mmsi: int,
    pings: List[Dict[str, Any]],
    all_vessel_positions: List[Dict[str, Any]],  # [{mmsi, lat, lon, vessel_type}]
    radius_m: float = PROXIMITY_RADIUS_M,
) -> List[Dict[str, Any]]:
    """
    Find other vessels within radius_m metres of this vessel at any point during the window.

    Uses O(n*m) pairwise check — acceptable for hackathon scale (tens to low hundreds of vessels).
    Each vessel appears at most once in the result even if it came close multiple times.

    Returns a list of {mmsi, distance_m, timestamp} dicts.
    """
    from shared.geo_utils import haversine_distance

    # Build a quick lookup: {other_mmsi: {lat, lon}}
    other_vessels = {v["mmsi"]: v for v in all_vessel_positions if v["mmsi"] != mmsi}
    if not other_vessels:
        return []

    closest_encounters: Dict[int, Dict[str, Any]] = {}  # other_mmsi -> closest encounter

    for ping in pings:
        my_lat = ping.get("lat")
        my_lon = ping.get("lon")
        if my_lat is None or my_lon is None:
            continue
        ts = ping.get("timestamp") or ping.get("ts_epoch")

        for other_mmsi, other in other_vessels.items():
            other_lat = other.get("lat")
            other_lon = other.get("lon")
            if other_lat is None or other_lon is None:
                continue

            d_m = haversine_distance(
                float(my_lat), float(my_lon),
                float(other_lat), float(other_lon)
            )
            if d_m <= radius_m:
                # Keep the closest encounter with each vessel
                if other_mmsi not in closest_encounters or d_m < closest_encounters[other_mmsi]["distance_m"]:
                    closest_encounters[other_mmsi] = {
                        "mmsi": other_mmsi,
                        "distance_m": round(d_m, 1),
                        "timestamp": str(ts),
                        "other_speed_knots": other.get("speed_knots"),
                    }

    return list(closest_encounters.values())


def compute_features(
    mmsi: int,
    pings: List[Dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    port_nearby: bool = False,
    all_vessel_positions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Compute the full behavioral feature vector for one vessel window.

    Returns a dict matching the vessel_features table schema.
    The 'quality' field indicates data sufficiency:
      - 'ok': >= 3 pings, all features computed
      - 'low': 2 pings, some features degraded
      - 'insufficient': 0-1 pings, only basic fields set
    """
    n = len(pings)
    base = {
        "mmsi": mmsi,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "ping_count": n,
        "avg_speed": None,
        "speed_variance": None,
        "max_speed": None,
        "course_variance": None,
        "heading_change_rate": None,
        "loitering_score": None,
        "distance_traveled_km": 0.0,
        "proximity_events": [],
        "quality": "ok",
        "max_speed_delta_kn": None,
        "max_speed_drop_kn": 0.0,
        "mean_cog_heading_divergence_deg": None,
        "max_cog_heading_divergence_deg": None,
        "repeated_cog_heading_divergences": 0,
        "max_rate_of_turn_deg_min": None,
        "turn_reversal_count": 0,
        "draught_m": None,
        "draught_change_m": None,
    }

    if n < 2:
        base["quality"] = "insufficient" if n == 0 else "low"
        if n == 1:
            speeds = _safe_speeds(pings)
            if speeds:
                base["avg_speed"] = speeds[0]
            base["loitering_score"] = compute_loitering_score(pings, port_nearby)
        return base

    # Speed statistics
    speeds = _safe_speeds(pings)
    if speeds:
        base["avg_speed"] = round(sum(speeds) / len(speeds), 2)
        base["max_speed"] = round(max(speeds), 2)
        if len(speeds) >= 2:
            mean_s = base["avg_speed"]
            variance = sum((s - mean_s) ** 2 for s in speeds) / len(speeds)
            base["speed_variance"] = round(variance, 4)
        else:
            base["speed_variance"] = 0.0
    else:
        base["quality"] = "low"  # no valid speed data

    # Course variance (circular — the correct implementation)
    courses = _safe_courses(pings)
    if len(courses) >= 2:
        base["course_variance"] = round(compute_circular_variance(courses), 2)

    # Heading change rate
    base["heading_change_rate"] = compute_heading_change_rate(pings)
    if base["heading_change_rate"] is not None:
        base["heading_change_rate"] = round(base["heading_change_rate"], 4)

    # Loitering score
    base["loitering_score"] = compute_loitering_score(pings, port_nearby)

    # Distance traveled
    base["distance_traveled_km"] = compute_distance_traveled_km(pings)

    # Proximity events
    if all_vessel_positions:
        base["proximity_events"] = compute_proximity_events(
            mmsi, pings, all_vessel_positions
        )

    base.update(compute_motion_integrity_features(pings))
    draughts = []
    for ping in pings:
        try:
            value = float(ping.get("draught"))
            if 0 <= value <= 30:
                draughts.append(value)
        except (TypeError, ValueError):
            continue
    if draughts:
        base["draught_m"] = draughts[-1]
        base["draught_change_m"] = round(draughts[-1] - draughts[0], 3)

    return base
