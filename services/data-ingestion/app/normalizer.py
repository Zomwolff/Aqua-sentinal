"""AIS timestamp and field normalisation for the data-ingestion service."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared.geo_utils import interpolate_position

log = logging.getLogger(__name__)


def normalize_timestamp(ts_raw: Any) -> Optional[datetime]:
    """
    Normalise a raw timestamp value to a UTC-aware datetime.

    Handles:
    - int/float: Unix epoch seconds (most AIS APIs)
    - str ISO-8601: '2024-01-15T10:30:00Z', '2024-01-15T10:30:00+05:30'
    - datetime: passed through (tz-aware or naive; naive assumed UTC)
    - str epoch: '1705315800' (some providers send epochs as strings)
    """
    if ts_raw is None:
        return None

    if isinstance(ts_raw, datetime):
        if ts_raw.tzinfo is None:
            return ts_raw.replace(tzinfo=timezone.utc)
        return ts_raw.astimezone(timezone.utc)

    if isinstance(ts_raw, (int, float)):
        # Detect millisecond epochs (> year 3000 in seconds = 32503680000)
        if ts_raw > 32_503_680_000:
            ts_raw /= 1000.0
        return datetime.fromtimestamp(ts_raw, tz=timezone.utc)

    if isinstance(ts_raw, str):
        # Try epoch string first
        try:
            epoch = float(ts_raw)
            return normalize_timestamp(epoch)
        except ValueError:
            pass
        # Try ISO-8601
        for fmt in (
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                dt = datetime.strptime(ts_raw, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        # Try datetime.fromisoformat (Python 3.11+ handles offset-aware)
        try:
            dt = datetime.fromisoformat(ts_raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass

    log.warning("Could not parse timestamp: %r", ts_raw)
    return None


def normalize_speed(speed_raw: Any, source_unit: str = "knots") -> Optional[float]:
    """
    Normalise vessel speed to knots.

    source_unit options: 'knots' (default), 'ms' (m/s), 'cms' (cm/s)
    AIS standard is always tenths-of-knots (integer * 0.1), but some
    satellite AIS APIs report in m/s or cm/s.
    """
    if speed_raw is None:
        return None
    try:
        v = float(speed_raw)
    except (ValueError, TypeError):
        return None
    if source_unit == "ms":
        v = v * 1.94384  # m/s -> knots
    elif source_unit == "cms":
        v = v * 0.0194384  # cm/s -> knots
    # AIS protocol uses 1023 (= 102.3 knots) to mean "speed not available"
    if v >= 102.2:
        return None
    return round(v, 1)


def normalize_course(course_raw: Any) -> Optional[float]:
    """Return course as a float in [0, 360), or None if not available."""
    if course_raw is None:
        return None
    try:
        c = float(course_raw)
    except (ValueError, TypeError):
        return None
    if c >= 360.0:
        return None  # AIS "not available"
    return round(c % 360.0, 1)


def normalize_heading(heading_raw: Any) -> Optional[int]:
    """Return heading as int in [0, 511], or None if not available / 511."""
    if heading_raw is None:
        return None
    try:
        h = int(float(heading_raw))
    except (ValueError, TypeError):
        return None
    if h == 511:
        return None  # AIS "not available"
    if not (0 <= h <= 360):
        return None
    return h


def fill_position_gaps(
    positions: List[Dict[str, Any]],
    target_interval_s: int = 60,
    max_gap_s: int = 3600,
) -> List[Dict[str, Any]]:
    """
    Fill temporal gaps between AIS position reports by linear interpolation.

    Only interpolates if:
    - Gap is > target_interval_s (no point interpolating between 5s pings)
    - Gap is <= max_gap_s (don't interpolate across multi-hour silences;
      those are genuine gaps that the anomaly detector should flag)

    Returns a new list with interpolated pings inserted, sorted by timestamp.
    Interpolated pings have quality_flag='interpolated'.
    """
    if len(positions) < 2:
        return list(positions)

    # Ensure sorted by timestamp
    sorted_pos = sorted(positions, key=lambda p: p["timestamp"])
    result: List[Dict[str, Any]] = [sorted_pos[0]]

    for i in range(1, len(sorted_pos)):
        prev = sorted_pos[i - 1]
        curr = sorted_pos[i]
        dt = (curr["timestamp"] - prev["timestamp"]).total_seconds()

        if target_interval_s < dt <= max_gap_s:
            # Insert interpolated pings
            n_steps = int(dt // target_interval_s)
            for step in range(1, n_steps):
                frac = step * target_interval_s
                target_ts = prev["timestamp"] + __import__("datetime").timedelta(seconds=frac)
                i_lat, i_lon = interpolate_position(
                    prev["lat"], prev["lon"], prev["timestamp"],
                    curr["lat"], curr["lon"], curr["timestamp"],
                    target_ts,
                )
                interp = dict(prev)  # copy all fields from previous
                interp["lat"] = i_lat
                interp["lon"] = i_lon
                interp["timestamp"] = target_ts
                interp["quality_flag"] = "interpolated"
                result.append(interp)

        result.append(curr)

    return result
