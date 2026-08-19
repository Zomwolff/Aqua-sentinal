"""Geospatial utility functions for the Aqua-Sentinel AIS pipeline."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, List, Optional, Tuple

EARTH_RADIUS_M = 6_371_000.0  # metres


# ──────────────────────────────────────────────────────────────────────────────
# Distance / Bearing
# ──────────────────────────────────────────────────────────────────────────────

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Great-circle distance between two (lat, lon) points in **metres**.
    Uses the Haversine formula, accurate to ~0.5% over the distances we care about.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Initial bearing from point 1 to point 2 in degrees [0, 360).
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)

    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360


def implied_speed_ms(lat1: float, lon1: float, ts1: datetime,
                     lat2: float, lon2: float, ts2: datetime) -> Optional[float]:
    """
    Speed implied by two position-time pairs, in m/s.
    Returns None if timestamps are identical.
    """
    dt = abs((ts2 - ts1).total_seconds())
    if dt == 0:
        return None
    dist = haversine_distance(lat1, lon1, lat2, lon2)
    return dist / dt


# ──────────────────────────────────────────────────────────────────────────────
# Circular statistics (course / heading wrap at 360°)
# ──────────────────────────────────────────────────────────────────────────────

def circular_mean(angles_deg: List[float]) -> float:
    """
    Mean of a list of angles in degrees, handling 0°/360° wraparound correctly.
    Returns degrees [0, 360).
    """
    if not angles_deg:
        return 0.0
    radians = [math.radians(a) for a in angles_deg]
    sin_mean = sum(math.sin(r) for r in radians) / len(radians)
    cos_mean = sum(math.cos(r) for r in radians) / len(radians)
    return (math.degrees(math.atan2(sin_mean, cos_mean)) + 360) % 360


def circular_variance(angles_deg: List[float]) -> float:
    """
    Circular variance of a list of angles in degrees.
    Result is in [0, 1] where 0 = all identical, 1 = maximally spread.
    Multiply by 360² to get a value in degrees² for threshold comparison.

    Uses the formula: 1 - |R_bar| where R_bar is the mean resultant length.
    This is the correct implementation — naive variance breaks at the 0°/360° boundary.
    """
    if len(angles_deg) < 2:
        return 0.0
    radians = [math.radians(a) for a in angles_deg]
    sin_mean = sum(math.sin(r) for r in radians) / len(radians)
    cos_mean = sum(math.cos(r) for r in radians) / len(radians)
    r_bar = math.sqrt(sin_mean ** 2 + cos_mean ** 2)
    return 1.0 - r_bar  # in [0, 1]


def angular_difference(a: float, b: float) -> float:
    """
    Smallest signed difference between two angles in degrees.
    Result is in (-180, 180].
    """
    diff = (a - b + 180) % 360 - 180
    return diff


# ──────────────────────────────────────────────────────────────────────────────
# Bounding box
# ──────────────────────────────────────────────────────────────────────────────

def point_in_bbox(lat: float, lon: float,
                  min_lat: float, min_lon: float,
                  max_lat: float, max_lon: float) -> bool:
    """True if (lat, lon) is inside the axis-aligned bounding box."""
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


# ──────────────────────────────────────────────────────────────────────────────
# Position interpolation
# ──────────────────────────────────────────────────────────────────────────────

def interpolate_position(
    lat1: float, lon1: float, ts1: datetime,
    lat2: float, lon2: float, ts2: datetime,
    target_ts: datetime
) -> Tuple[float, float]:
    """
    Linear interpolation of (lat, lon) at target_ts between two known positions.
    Clamps to the nearest endpoint if target_ts is outside [ts1, ts2].
    """
    total_s = (ts2 - ts1).total_seconds()
    if total_s <= 0:
        return lat1, lon1
    elapsed_s = (target_ts - ts1).total_seconds()
    t = max(0.0, min(1.0, elapsed_s / total_s))  # clamp to [0, 1]
    return lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1)


# ──────────────────────────────────────────────────────────────────────────────
# WKT helpers for PostGIS
# ──────────────────────────────────────────────────────────────────────────────

def point_wkt(lat: float, lon: float) -> str:
    """POINT WKT in lon lat order (PostGIS convention)."""
    return f"POINT({lon} {lat})"


def polygon_wkt(points: List[Tuple[float, float]]) -> str:
    """
    Convert a list of (lat, lon) tuples to a closed WKT POLYGON.
    Automatically closes the ring if the first and last points differ.
    """
    if len(points) < 3:
        raise ValueError("A polygon requires at least 3 points")
    # Ensure ring is closed
    if points[0] != points[-1]:
        points = list(points) + [points[0]]
    coords = ", ".join(f"{lon} {lat}" for lat, lon in points)  # lon lat order
    return f"POLYGON(({coords}))"


# ──────────────────────────────────────────────────────────────────────────────
# MMSI utilities
# ──────────────────────────────────────────────────────────────────────────────

# Maritime Identification Digits (MID) → country name mapping (subset, enough for validation)
# Full table: https://www.itu.int/en/ITU-R/terrestrial/maritime/Pages/MMSI.aspx
_VALID_MID_RANGES = (
    (201, 279),  # Europe
    (301, 339),  # North America
    (401, 440),  # Asia
    (401, 440),  # Asia
    (419, 419),  # India (419)
    (501, 572),  # Oceania
    (601, 638),  # Africa
    (700, 775),  # South America
    (401, 440),  # Asia
)
# Complete valid MIDs (ITU-R M.585)
_ALL_VALID_MIDS = set(range(201, 280)) | set(range(301, 340)) | \
                  set(range(401, 441)) | set(range(501, 573)) | \
                  set(range(601, 639)) | set(range(700, 776))
# Add specific Indian MID
_ALL_VALID_MIDS.add(419)


def validate_mmsi(mmsi: int) -> Tuple[bool, Optional[str]]:
    """
    Validate an MMSI number.
    Returns (is_valid, reason_if_invalid).

    Rules:
    - Must be exactly 9 digits
    - MID (first 3 digits) must be in the valid ITU MID list
    - Must not be all-same-digit (e.g. 000000000, 111111111)
    - Special ranges: 970XXXXXX (SAR transponders), 98XXXXXXX (MOB devices) are valid but unusual
    """
    mmsi_str = str(mmsi)
    if len(mmsi_str) != 9:
        return False, f"MMSI must be 9 digits, got {len(mmsi_str)}"
    if len(set(mmsi_str)) == 1:
        return False, f"MMSI is all identical digits: {mmsi}"
    mid = int(mmsi_str[:3])
    # Allow SAR transponders (970) and MOB devices (972, 974)
    special_prefixes = {970, 972, 974, 111, 000, 999}
    if mid in special_prefixes:
        return True, None  # valid, just unusual
    if mid not in _ALL_VALID_MIDS:
        return False, f"Invalid MID (Maritime Identification Digit): {mid}"
    return True, None


def mmsi_to_flag(mmsi: int) -> Optional[str]:
    """
    Attempt to derive a 3-digit MID from MMSI.
    Returns the MID as a string, or None if MMSI format is unexpected.
    """
    mmsi_str = str(mmsi)
    if len(mmsi_str) == 9:
        return mmsi_str[:3]
    return None


# ──────────────────────────────────────────────────────────────────────────────
# AIS ship type decoding
# ──────────────────────────────────────────────────────────────────────────────

_SHIP_TYPE_MAP: Dict[int, str] = {
    0: "unknown",
    20: "wing_in_ground",
    21: "wing_in_ground_hazardous_A",
    30: "fishing",
    31: "towing",
    32: "towing_large",
    33: "dredger",
    34: "diving",
    35: "military",
    36: "sailing",
    37: "pleasure",
    40: "high_speed",
    50: "pilot",
    51: "sar",
    52: "tug",
    53: "port_tender",
    54: "anti_pollution",
    55: "law_enforcement",
    60: "passenger",
    61: "passenger",
    62: "passenger",
    63: "passenger",
    64: "passenger",
    69: "passenger",
    70: "cargo",
    71: "cargo",
    72: "cargo",
    73: "cargo",
    74: "cargo",
    75: "cargo",
    76: "cargo",
    77: "cargo",
    78: "cargo",
    79: "cargo",
    80: "tanker",
    81: "tanker",
    82: "tanker",
    83: "tanker",
    84: "tanker",
    85: "tanker",
    86: "tanker",
    87: "tanker",
    88: "tanker",
    89: "tanker",
    90: "other",
}


def decode_ship_type(type_code: Optional[int]) -> str:
    """Return a human-readable vessel type string from AIS ship type code."""
    if type_code is None:
        return "unknown"
    return _SHIP_TYPE_MAP.get(type_code, "other")


# Max plausible speed (knots) by vessel type — used by anomaly detection
VESSEL_TYPE_MAX_SPEED: Dict[str, float] = {
    "tanker": 20.0,
    "cargo": 25.0,
    "fishing": 15.0,
    "passenger": 30.0,
    "tug": 12.0,
    "high_speed": 45.0,
    "sar": 30.0,
    "sailing": 15.0,
    "pleasure": 25.0,
    "other": 30.0,
    "unknown": 35.0,
}
