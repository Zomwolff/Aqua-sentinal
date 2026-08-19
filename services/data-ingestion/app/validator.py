"""AIS record validation pipeline for the data-ingestion service."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from shared.geo_utils import point_in_bbox, validate_mmsi

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (read at import time; call refresh_config() if env changes)
# ---------------------------------------------------------------------------

import os

BBOX = {
    "min_lat": float(os.environ.get("AIS_BBOX_MIN_LAT", 14.0)),
    "min_lon": float(os.environ.get("AIS_BBOX_MIN_LON", 68.0)),
    "max_lat": float(os.environ.get("AIS_BBOX_MAX_LAT", 25.0)),
    "max_lon": float(os.environ.get("AIS_BBOX_MAX_LON", 77.5)),
}

# AIS special sentinel values that mean "not available"
AIS_SPEED_NOT_AVAILABLE = {102.2, 102.3}
AIS_COURSE_NOT_AVAILABLE = 360.0  # or > 360
AIS_HEADING_NOT_AVAILABLE = 511

# Plausible speed range in knots
MIN_SPEED = 0.0
MAX_SPEED = 102.2  # AIS protocol max
SUSPICIOUS_SPEED = 40.0  # flag but don't drop

# Max age of a valid AIS record (reject older to prevent replay)
MAX_STALENESS_HOURS = 24
# Max seconds into the future we tolerate (NTP drift / clock skew)
MAX_FUTURE_SECONDS = 300

# Valid AIS navigational status codes (0-15)
VALID_NAV_STATUS = set(range(0, 16))


def validate_ais_record(record: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Validate a raw AIS record dict.
    Returns (True, None) if valid, or (False, reason_string) if invalid.

    This is the single validation gate: every record MUST pass before it
    is forwarded to downstream services. Be strict on structural issues;
    be permissive (flag, don't drop) on value-range oddities that could
    legitimately appear in real data.
    """
    # ── 1. Required fields ────────────────────────────────────────────────────
    for field in ("mmsi", "lat", "lon", "timestamp"):
        if record.get(field) is None:
            return False, f"missing_required_field:{field}"

    # ── 2. MMSI validity ─────────────────────────────────────────────────────
    try:
        mmsi = int(record["mmsi"])
    except (ValueError, TypeError):
        return False, "mmsi_not_integer"

    mmsi_ok, mmsi_reason = validate_mmsi(mmsi)
    if not mmsi_ok:
        return False, f"invalid_mmsi:{mmsi_reason}"

    # ── 3. Coordinate ranges ─────────────────────────────────────────────────
    try:
        lat = float(record["lat"])
        lon = float(record["lon"])
    except (ValueError, TypeError):
        return False, "coords_not_numeric"

    if not (-90.0 <= lat <= 90.0):
        return False, f"lat_out_of_range:{lat}"
    if not (-180.0 <= lon <= 180.0):
        return False, f"lon_out_of_range:{lon}"

    # Reject records at the AIS default "no-fix" coordinates
    # (91.0,181.0) means GPS not available in AIS spec
    if lat == 91.0 or lon == 181.0:
        return False, "coords_are_ais_default_no_fix"

    # ── 4. Bounding box filter ────────────────────────────────────────────────
    if not point_in_bbox(lat, lon,
                          BBOX["min_lat"], BBOX["min_lon"],
                          BBOX["max_lat"], BBOX["max_lon"]):
        return False, "outside_area_of_interest"

    # ── 5. Timestamp validity ─────────────────────────────────────────────────
    ts = record["timestamp"]
    if not isinstance(ts, datetime):
        return False, "timestamp_not_datetime_object"

    # Normalise to UTC-aware for comparison
    now_utc = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts_utc = ts.replace(tzinfo=timezone.utc)
    else:
        ts_utc = ts.astimezone(timezone.utc)

    if ts_utc > now_utc + timedelta(seconds=MAX_FUTURE_SECONDS):
        return False, f"timestamp_in_future:{ts_utc.isoformat()}"

    if ts_utc < now_utc - timedelta(hours=MAX_STALENESS_HOURS):
        return False, f"timestamp_too_stale:{ts_utc.isoformat()}"

    # ── 6. Speed (permissive — flag unusual values, don't drop) ──────────────
    speed = record.get("speed_knots")
    if speed is not None:
        try:
            speed = float(speed)
            if speed < MIN_SPEED or speed > MAX_SPEED:
                # Clip to valid range rather than reject
                record["speed_knots"] = max(MIN_SPEED, min(MAX_SPEED, speed))
                record["quality_flag"] = "corrected"
        except (ValueError, TypeError):
            record["speed_knots"] = None

    # ── 7. Course validity ────────────────────────────────────────────────────
    course = record.get("course")
    if course is not None:
        try:
            course = float(course)
            if course > 360.0:
                record["course"] = None  # AIS "not available"
        except (ValueError, TypeError):
            record["course"] = None

    # ── 8. Heading validity ───────────────────────────────────────────────────
    heading = record.get("heading")
    if heading is not None:
        try:
            heading = int(heading)
            if heading < 0 or heading > 511:
                record["heading"] = None
        except (ValueError, TypeError):
            record["heading"] = None

    # ── 9. Nav status validity ────────────────────────────────────────────────
    nav_status = record.get("nav_status")
    if nav_status is not None:
        try:
            nav_status = int(nav_status)
            if nav_status not in VALID_NAV_STATUS:
                record["nav_status"] = None
        except (ValueError, TypeError):
            record["nav_status"] = None

    return True, None
