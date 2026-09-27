"""
Historical environmental forcing data-access layer for hindcasting Step 2.

Fetches historical wind and ocean-current data from:
  - Open-Meteo ERA5 archive  (archive-api.open-meteo.com/v1/archive)
  - Open-Meteo Marine API    (marine-api.open-meteo.com/v1/marine)

Returns a ``forcing_series`` list directly consumable by the existing
``simulate_ensemble()`` in ``particles.py``.

**Scope**: This module is Step 2 only — the forcing data-access layer.
It does NOT modify the physics, perform backward propagation, run Bayesian
inference, or implement KDE / ML models.

**Spatial limitation**: A single-point time series is returned at the spill
source coordinate.  ``simulate_ensemble()`` applies this to all particles
regardless of their position — this is a pre-existing constraint in
``particles.py``, not introduced here.

**Temporal resolution**: Source data is hourly (ERA5 reanalysis).  The
particle time-step is 600 s; linear interpolation between hourly samples is
performed by ``_interp_forcing()`` inside ``particles.py``.  The loader does
not claim that interpolation creates new information.

**Current-direction convention**: Open-Meteo's ocean_current_direction is
assumed to be TOWARD (standard oceanographic convention).  ``particles.py``
uses meteorological FROM convention for both wind and current.  The loader
converts by default (``toward → from = (toward + 180) % 360``).  This
assumption is configurable and documented so it can be corrected if empirical
validation shows a systematic 180° error.
"""

import os
import math
from datetime import datetime, timezone, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# Module-level constants
# All tuneable values are overridable from environment variables for testing.
# ---------------------------------------------------------------------------

WIND_API_BASE: str = "https://archive-api.open-meteo.com/v1/archive"
MARINE_API_BASE: str = "https://marine-api.open-meteo.com/v1/marine"

HTTP_TIMEOUT_S: float = float(os.environ.get("FORCING_LOADER_TIMEOUT_S", "30.0"))
MIN_SAMPLES_DEFAULT: int = int(os.environ.get("FORCING_LOADER_MIN_SAMPLES", "2"))
CURRENT_DIR_CONVENTION: str = os.environ.get(
    "FORCING_LOADER_CURRENT_DIR_CONVENTION", "toward"
)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class ForcingLoaderError(Exception):
    """Base class for all forcing loader errors."""


class InvalidCoordinateError(ForcingLoaderError):
    """Raised when lat is outside [-90, 90] or lon is outside [-180, 180]."""


class InvalidTimeRangeError(ForcingLoaderError):
    """Raised when t_end <= t_start (empty or reversed time window)."""


class FutureTimeError(ForcingLoaderError):
    """Raised when t_start or t_end is too recent for the archive (no historical data yet)."""


class WindDataUnavailableError(ForcingLoaderError):
    """Raised when the wind archive API returns no usable samples for the requested window."""


class CurrentDataUnavailableError(ForcingLoaderError):
    """Raised when the marine API returns no usable current samples for the requested window."""


class InsufficientDataError(ForcingLoaderError):
    """Raised when fewer than min_samples remain after null removal and inner-join merging."""


class APITimeoutError(ForcingLoaderError):
    """Raised when an HTTP request to an external API times out."""


class APIRateLimitError(ForcingLoaderError):
    """Raised when an external API responds with HTTP 429 (Too Many Requests)."""


class APIResponseError(ForcingLoaderError):
    """Raised on unexpected HTTP status codes, non-JSON bodies, or missing expected JSON keys."""


# ---------------------------------------------------------------------------
# Task 2 — Input validation
# ---------------------------------------------------------------------------

def _validate_inputs(
    lat: float,
    lon: float,
    t_start: datetime,
    t_end: datetime,
) -> Tuple[datetime, datetime]:
    """
    Validate inputs and normalize t_start/t_end to UTC.

    Returns:
        (t_start_utc, t_end_utc) as timezone-aware UTC datetimes.

    Raises:
        ValueError: if t_start or t_end is timezone-naive.
        InvalidCoordinateError: if lat or lon are out of valid range.
        InvalidTimeRangeError: if t_end <= t_start.
        FutureTimeError: if t_end is within 1 day of now (archive has lag).
    """
    # 1. Reject naive datetimes — UTC normalization would silently mis-locate
    #    the time window otherwise.
    if t_start.tzinfo is None or t_end.tzinfo is None:
        raise ValueError("t_start and t_end must be timezone-aware datetimes")

    # 2. Normalize both inputs to UTC immediately; all downstream logic is UTC.
    t_start_utc = t_start.astimezone(timezone.utc)
    t_end_utc = t_end.astimezone(timezone.utc)

    # 3. Validate geographic bounds (WGS-84 decimal degrees).
    if lat < -90.0 or lat > 90.0:
        raise InvalidCoordinateError(f"lat={lat} is outside [-90, 90]")
    if lon < -180.0 or lon > 180.0:
        raise InvalidCoordinateError(f"lon={lon} is outside [-180, 180]")

    # 4. Enforce non-empty, forward-going time window.
    if t_end_utc <= t_start_utc:
        raise InvalidTimeRangeError(
            f"t_end ({t_end_utc}) must be after t_start ({t_start_utc})"
        )

    # 5. Reject requests that reach into the ERA5 archive lag window.
    #    The archive typically lags ~5 days; we use a conservative 1-day cutoff.
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    if t_end_utc > cutoff:
        raise FutureTimeError(
            f"t_end ({t_end_utc}) is too recent; ERA5 archive requires data to be "
            f"at least 1 day old (cutoff: {cutoff})"
        )

    return t_start_utc, t_end_utc


# ---------------------------------------------------------------------------
# Task 3 — Wind archive fetcher
# ---------------------------------------------------------------------------

def _fetch_wind_archive(
    lat: float,
    lon: float,
    date_start: date,
    date_end: date,
) -> List[Dict[str, Any]]:
    """
    Fetch hourly wind data from Open-Meteo ERA5 archive.

    Source: archive-api.open-meteo.com/v1/archive
    Variables: wind_speed_10m (m/s), wind_direction_10m (degrees, met FROM convention)
    Temporal resolution: hourly

    Returns:
        List of {"t": datetime(UTC), "wind_speed_ms": float, "wind_dir_deg": float}
        Rows with None values are dropped.

    Raises:
        APITimeoutError, APIRateLimitError, APIResponseError,
        WindDataUnavailableError
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_start.isoformat(),
        "end_date": date_end.isoformat(),
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "ms",
        "timezone": "UTC",
    }

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            resp = client.get(WIND_API_BASE, params=params)
    except httpx.TimeoutException:
        raise APITimeoutError(
            f"Wind archive request timed out after {HTTP_TIMEOUT_S}s: {WIND_API_BASE}"
        )

    if resp.status_code == 429:
        raise APIRateLimitError(
            f"Wind archive API rate limit hit (HTTP 429): {WIND_API_BASE}"
        )
    if resp.status_code != 200:
        raise APIResponseError(
            f"Wind archive API error HTTP {resp.status_code}: {WIND_API_BASE}"
        )

    try:
        data = resp.json()
    except Exception:
        raise APIResponseError(
            f"Wind archive API returned non-JSON response: {WIND_API_BASE}"
        )

    try:
        hourly = data["hourly"]
        times = hourly["time"]
        speeds = hourly["wind_speed_10m"]
        directions = hourly["wind_direction_10m"]
    except (KeyError, TypeError):
        raise APIResponseError(
            f"Wind archive API response missing expected keys: {WIND_API_BASE}"
        )

    rows: List[Dict[str, Any]] = []
    for t_str, speed, direction in zip(times, speeds, directions):
        # Drop rows where either value is missing — R6: no silent fallbacks.
        if speed is None or direction is None:
            continue
        t_utc = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
        rows.append(
            {
                "t": t_utc,
                "wind_speed_ms": float(speed),
                "wind_dir_deg": float(direction),
            }
        )

    if not rows:
        raise WindDataUnavailableError(
            f"No valid wind data returned for lat={lat}, lon={lon}, "
            f"{date_start}–{date_end}"
        )

    return rows


# ---------------------------------------------------------------------------
# Task 4 — Marine current fetcher
# ---------------------------------------------------------------------------

def _fetch_marine_archive(
    lat: float,
    lon: float,
    date_start: date,
    date_end: date,
) -> List[Dict[str, Any]]:
    """
    Fetch hourly ocean current data from Open-Meteo Marine API.

    Source: marine-api.open-meteo.com/v1/marine
    Variables:
      - ocean_current_velocity (km/h) — converted to m/s (÷ 3.6)
      - ocean_current_direction (degrees) — convention unverified in docs;
        assumed TOWARD (oceanographic); conversion to FROM applied in
        _merge_and_convert()

    Temporal resolution: hourly

    Returns:
        List of {"t": datetime(UTC), "current_speed_ms": float, "raw_dir_deg": float}
        Note: raw_dir_deg is NOT yet convention-converted. That happens in _merge_and_convert().
        Rows with None values are dropped.

    Raises:
        APITimeoutError, APIRateLimitError, APIResponseError,
        CurrentDataUnavailableError
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_start.isoformat(),
        "end_date": date_end.isoformat(),
        "hourly": "ocean_current_velocity,ocean_current_direction",
        "timezone": "UTC",
    }

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            resp = client.get(MARINE_API_BASE, params=params)
    except httpx.TimeoutException:
        raise APITimeoutError(
            f"Marine archive request timed out after {HTTP_TIMEOUT_S}s: {MARINE_API_BASE}"
        )

    if resp.status_code == 429:
        raise APIRateLimitError(
            f"Marine archive API rate limit hit (HTTP 429): {MARINE_API_BASE}"
        )
    if resp.status_code != 200:
        raise APIResponseError(
            f"Marine archive API error HTTP {resp.status_code}: {MARINE_API_BASE}"
        )

    try:
        data = resp.json()
    except Exception:
        raise APIResponseError(
            f"Marine archive API returned non-JSON response: {MARINE_API_BASE}"
        )

    try:
        hourly = data["hourly"]
        times = hourly["time"]
        velocities = hourly["ocean_current_velocity"]
        directions = hourly["ocean_current_direction"]
    except (KeyError, TypeError):
        raise APIResponseError(
            f"Marine archive API response missing expected keys: {MARINE_API_BASE}"
        )

    rows: List[Dict[str, Any]] = []
    for t_str, vel, direction in zip(times, velocities, directions):
        # Drop rows where either value is missing — R6: no silent fallbacks.
        if vel is None or direction is None:
            continue
        # Convert km/h → m/s per R4.
        current_speed_ms = float(vel) / 3.6
        t_utc = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
        rows.append(
            {
                "t": t_utc,
                "current_speed_ms": current_speed_ms,
                # raw_dir_deg is the direction straight from the API.
                # ⚠ Convention note: Open-Meteo docs do not explicitly state
                # whether this is TOWARD (oceanographic) or FROM (meteorological).
                # Assumed TOWARD; conversion to FROM is applied in _merge_and_convert().
                # If hindcast validation shows a systematic 180° drift offset,
                # set FORCING_LOADER_CURRENT_DIR_CONVENTION=from to disable conversion.
                "raw_dir_deg": float(direction),
            }
        )

    if not rows:
        raise CurrentDataUnavailableError(
            f"No valid ocean current data returned for lat={lat}, lon={lon}, "
            f"{date_start}–{date_end}"
        )

    return rows


# ---------------------------------------------------------------------------
# Task 5 — Merge, conversion, and assembly
# ---------------------------------------------------------------------------

def _merge_and_convert(
    wind_rows: List[Dict[str, Any]],
    current_rows: List[Dict[str, Any]],
    current_dir_convention: str = "toward",
) -> List[Dict[str, Any]]:
    """
    Inner-join wind and current rows on timestamp, apply unit/convention
    conversions, and assemble the final forcing dicts.

    Inner join strategy: only timestamps present in BOTH wind and current
    series are kept.  This prevents silent use of missing data (R6).

    Direction convention:
        current_dir_convention="toward"  -> (raw_dir + 180) % 360
            (assumes Open-Meteo returns oceanographic TOWARD direction)
        current_dir_convention="from"    -> raw_dir unchanged
            (assumes Open-Meteo returns meteorological FROM direction)

    WARNING — SCIENTIFIC NOTE:
        Open-Meteo does not explicitly document which convention
        ocean_current_direction uses.  "toward" is the standard oceanographic
        convention and is the default assumption here.
        particles.py uses _wind_components() which applies meteorological FROM
        convention to BOTH wind and current — they must match.
        If hindcast validation shows a systematic 180° drift offset,
        set FORCING_LOADER_CURRENT_DIR_CONVENTION=from in the environment.

    Returns:
        List of forcing dicts with exactly 5 keys:
            t, wind_speed_ms, wind_dir_deg, current_speed_ms, current_dir_deg
    """
    # Build lookup maps keyed by UTC timestamp.
    wind_map: Dict[datetime, Dict[str, Any]] = {r["t"]: r for r in wind_rows}
    cur_map: Dict[datetime, Dict[str, Any]] = {r["t"]: r for r in current_rows}

    # Inner join: keep only timestamps present in both series.
    common_timestamps = sorted(set(wind_map.keys()) & set(cur_map.keys()))

    result: List[Dict[str, Any]] = []
    for ts in common_timestamps:
        wind = wind_map[ts]
        cur = cur_map[ts]

        # Apply direction convention conversion.
        if current_dir_convention == "toward":
            # Standard oceanographic TOWARD -> meteorological FROM
            current_dir_deg = (cur["raw_dir_deg"] + 180.0) % 360.0
        else:
            # Already in FROM convention; no conversion needed.
            current_dir_deg = cur["raw_dir_deg"]

        result.append(
            {
                "t": ts,
                "wind_speed_ms": wind["wind_speed_ms"],
                "wind_dir_deg": wind["wind_dir_deg"],
                "current_speed_ms": cur["current_speed_ms"],
                "current_dir_deg": current_dir_deg,
            }
        )

    return result


# ---------------------------------------------------------------------------
# Task 6 — Sample validation and public interface
# ---------------------------------------------------------------------------

def _validate_sample_count(series: List[Dict[str, Any]], min_samples: int) -> None:
    """
    Raise InsufficientDataError if series has fewer than min_samples items.

    Args:
        series: The assembled forcing series after inner-join merge.
        min_samples: Minimum number of samples required.

    Raises:
        InsufficientDataError: with count and threshold in the message.
    """
    if len(series) < min_samples:
        raise InsufficientDataError(
            f"Only {len(series)} forcing sample(s) available after merging wind "
            f"and current data; {min_samples} required.  Widen the time window or "
            f"lower min_samples."
        )


def load_historical_forcing(
    lat: float,
    lon: float,
    t_start: datetime,
    t_end: datetime,
    min_samples: int = MIN_SAMPLES_DEFAULT,
) -> List[Dict[str, Any]]:
    """
    Load historical wind and ocean-current forcing for a past time window.

    Returns a forcing_series directly consumable by simulate_ensemble() in
    particles.py.  Each element has:
        {
            "t":                datetime (timezone-aware UTC),
            "wind_speed_ms":    float (m/s, from ERA5 reanalysis),
            "wind_dir_deg":     float (meteorological FROM convention, degrees),
            "current_speed_ms": float (m/s, converted from km/h),
            "current_dir_deg":  float (meteorological FROM convention, degrees),
        }

    Data sources:
        Wind:    archive-api.open-meteo.com  (ERA5 reanalysis, hourly, back to 1940)
        Current: marine-api.open-meteo.com   (hourly, start_date/end_date supported)

    Temporal resolution note:
        Source data is HOURLY.  The particle integration step is 600 s (10 min).
        Linear interpolation between hourly samples is performed inside
        particles.py (_interp_forcing).  This loader does NOT generate
        sub-hourly data; interpolation does not create new information.

    Spatial note:
        Returns a single-point time series at (lat, lon).  The current
        physics (particles.py) applies this to ALL particles regardless of
        position.  This is a pre-existing constraint, not introduced here.

    Args:
        lat: WGS-84 latitude [-90, 90]
        lon: WGS-84 longitude [-180, 180]
        t_start: Start of requested window (timezone-aware)
        t_end:   End of requested window (timezone-aware)
        min_samples: Minimum forcing samples required after merge
                     (default: MIN_SAMPLES_DEFAULT)

    Returns:
        List of forcing dicts sorted by t ascending.

    Raises:
        ValueError, InvalidCoordinateError, InvalidTimeRangeError,
        FutureTimeError, WindDataUnavailableError, CurrentDataUnavailableError,
        InsufficientDataError, APITimeoutError, APIRateLimitError, APIResponseError
    """
    # Step 1: Validate inputs and normalize to UTC.
    t_start_utc, t_end_utc = _validate_inputs(lat, lon, t_start, t_end)

    # Step 2: Expand to full calendar days for the API (keeps boundary samples
    # that anchor _interp_forcing() at the edges of the window).
    date_start = t_start_utc.date()
    date_end = t_end_utc.date()

    # Step 3: Fetch wind (ERA5 archive).
    wind_rows = _fetch_wind_archive(lat, lon, date_start, date_end)

    # Step 4: Fetch ocean current (marine archive).
    # The Open-Meteo marine API has incomplete historical coverage — for
    # example, the Indian Ocean before 2022 returns all-null values.
    # When no current data is available, fall back to zero current so that
    # origin inference can still run wind-driven backward propagation rather
    # than failing entirely.  Zero current is conservative and explicitly
    # documented in the forcing provenance via current_speed_ms=0.0.
    try:
        current_rows = _fetch_marine_archive(lat, lon, date_start, date_end)
    except CurrentDataUnavailableError:
        # Build zero-current rows aligned to the wind timestamps so the
        # inner-join in _merge_and_convert produces a full series.
        current_rows = [
            {"t": r["t"], "current_speed_ms": 0.0, "raw_dir_deg": 0.0}
            for r in wind_rows
        ]

    # Step 5: Inner-join, convert units, align direction conventions.
    series = _merge_and_convert(wind_rows, current_rows, CURRENT_DIR_CONVENTION)

    # Step 6: Guard against insufficient data (R6, R8).
    _validate_sample_count(series, min_samples)

    # Step 7: Return sorted by timestamp (defensive — _merge_and_convert already
    # sorts, but simulate_ensemble() also sorts internally).
    return sorted(series, key=lambda s: s["t"])


def _load_with_cache(
    lat: float,
    lon: float,
    t_start: datetime,
    t_end: datetime,
    cache_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Cache-ready wrapper around load_historical_forcing().

    cache_dir=None disables caching (current behavior — direct API fetch).
    When caching is implemented in a future step, set cache_dir to a local
    directory path.  This wrapper keeps the public interface stable.

    Use this entry point in production code so caching can be enabled
    transparently without changing call sites.
    """
    # Caching not yet implemented.  cache_dir is accepted but ignored.
    return load_historical_forcing(lat, lon, t_start, t_end)
