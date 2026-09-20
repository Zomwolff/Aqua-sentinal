"""
CMEMS Historical Ocean-Current Loader — cmems_current_loader.py

PURPOSE
=======
Loads GLORYS12V1 daily-mean ocean-current reanalysis from a local NetCDF file
(downloaded from Copernicus Marine Service, GLOBAL_MULTIYEAR_PHY_001_030) and
returns current forcing in the format consumed by the Aqua-Sentinel hindcast
pipeline.

DATASET
=======
Product:  cmems_mod_glo_phy_my_0.083deg_P1D-m  (GLORYS12V1)
Source:   MERCATOR / Copernicus Marine Service
Variables: uo (eastward, m/s), vo (northward, m/s)
Depth:    0.494 m (shallowest available surface level)
Grid:     1/12° (~8 km), regular lat/lon
Coverage: (for the downloaded MSC Chitra file) 2010-08-05 – 2010-08-12,
          lat 18.5–19.5°N, lon 72.0–73.5°E

DIRECTION CONVENTION — CRITICAL
================================
uo and vo are velocity VECTOR COMPONENTS, not directional angles.
    uo > 0  →  water flowing eastward
    vo > 0  →  water flowing northward

These are NOT meteorological FROM-directions. They must NOT be converted
using _wind_components() from particles.py. They are used directly as
east/north velocity components.

The existing pipeline's _interp_forcing() and _wind_components() are designed
for speed + direction in meteorological FROM convention (wind and current).
To pass CMEMS uo/vo into the pipeline via the standard forcing dict schema,
we convert the vector components to speed + direction-toward, then store them
as current_speed_ms and current_dir_deg. The forcing_loader.py normally
converts TOWARD → FROM (adding 180°) when the convention is "toward". Here
we store direction_toward directly in the output dict with a documented key
"current_dir_convention: toward", so the caller knows to configure the loader
accordingly.

TEMPORAL RESOLUTION LIMITATION
================================
This dataset provides DAILY MEAN currents (1 value per day).
The particle integration step is 600 s (10 minutes). Linear interpolation
between daily fields is applied to produce sub-daily forcing. This interpolation
does NOT create new physical information — it is purely a smooth transition
between consecutive daily means, suitable for operational drift modelling
but not equivalent to hourly reanalysis products like CMEMS 1/24° hourly.
This limitation must be carried in any validation report using this forcing.

SPATIAL INTERPOLATION
======================
Bilinear interpolation on the 1/12° CMEMS grid using scipy.interpolate.RegularGridInterpolator.
NaN cells (land/coastal masking) are filled by nearest-valid-cell before
interpolation. If all surrounding cells are NaN, the query raises OutOfDomainError.

WHAT THIS MODULE DOES NOT DO
=============================
- Does NOT modify simulate_ensemble() or any existing physics
- Does NOT replace the existing Open-Meteo wind loader (forcing_loader.py)
- Does NOT produce wind data — only ocean current
- The caller is responsible for combining wind (from forcing_loader.py) and
  current (from this module) into a merged forcing_series
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import xarray as xr
    from scipy.interpolate import RegularGridInterpolator
    _XARRAY_AVAILABLE = True
except ImportError:
    _XARRAY_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────

class CmemsCurrentError(Exception):
    """Base exception for CMEMS current loader."""


class OutOfDomainError(CmemsCurrentError):
    """Raised when query lat/lon falls outside the downloaded grid extent."""


class OutOfTimeRangeError(CmemsCurrentError):
    """Raised when query time is outside the dataset's temporal coverage."""


class NcFileNotFoundError(CmemsCurrentError):
    """Raised when the NetCDF file does not exist."""


# ─────────────────────────────────────────────────────────────────
# CmecsCurrentLoader
# ─────────────────────────────────────────────────────────────────

class CmemsCurrentLoader:
    """
    Loads GLORYS12V1 daily-mean ocean currents and provides interpolated
    (lat, lon, t) → (current_east_ms, current_north_ms) queries.

    Usage:
        loader = CmemsCurrentLoader("path/to/msc_chitra_currents.nc")
        uo, vo = loader.query(lat=18.96, lon=72.81,
                              t=datetime(2010, 8, 7, 4, 20, tzinfo=timezone.utc))

    or, to produce a forcing series compatible with the hindcast pipeline:
        series = loader.build_forcing_series(lat=18.96, lon=72.81,
                                             t_start=..., t_end=...)
    """

    def __init__(self, nc_path: str | Path) -> None:
        """
        Load and cache the CMEMS NetCDF dataset.

        Parameters
        ----------
        nc_path : str or Path
            Path to the downloaded msc_chitra_currents.nc (or equivalent).

        Raises
        ------
        NcFileNotFoundError
            If the file does not exist.
        ImportError
            If xarray or scipy are not installed.
        """
        if not _XARRAY_AVAILABLE:
            raise ImportError(
                "xarray and scipy are required for CmemsCurrentLoader. "
                "Install with: pip install xarray scipy netCDF4"
            )

        nc_path = Path(nc_path)
        if not nc_path.exists():
            raise NcFileNotFoundError(f"NetCDF file not found: {nc_path}")

        self._nc_path = nc_path
        self._load()

    def _load(self) -> None:
        """Parse the NetCDF and build bilinear interpolators per time step."""
        ds = xr.open_dataset(self._nc_path)

        # Squeeze depth dimension (single surface level)
        uo_data = ds["uo"].squeeze("depth").values   # shape (T, lat, lon)
        vo_data = ds["vo"].squeeze("depth").values

        # Grid axes
        self._lats = ds["latitude"].values.astype(float)
        self._lons = ds["longitude"].values.astype(float)

        # Parse times to UTC-aware datetimes
        raw_times = ds["time"].values   # numpy datetime64
        self._times_dt = [
            datetime(
                int(str(t)[:4]),
                int(str(t)[5:7]),
                int(str(t)[8:10]),
                tzinfo=timezone.utc,
            )
            for t in np.datetime_as_datetime(raw_times, unit="s")
        ] if hasattr(np, "datetime_as_datetime") else [
            datetime.fromtimestamp(
                float(np.datetime64(t, "s").astype("float64")), tz=timezone.utc
            )
            for t in raw_times
        ]
        self._times_ts = np.array([t.timestamp() for t in self._times_dt])

        # Domain bounds (with small tolerance)
        self._lat_min = float(self._lats.min()) - 0.001
        self._lat_max = float(self._lats.max()) + 0.001
        self._lon_min = float(self._lons.min()) - 0.001
        self._lon_max = float(self._lons.max()) + 0.001

        # Fill NaN (land/mask) cells using nearest valid
        uo_filled = self._fill_nans(uo_data.copy())
        vo_filled = self._fill_nans(vo_data.copy())

        # Build one RegularGridInterpolator per time step
        # (bilinear in lat/lon — RegularGridInterpolator with 'linear' method)
        self._uo_interps = []
        self._vo_interps = []
        for t_idx in range(len(self._times_dt)):
            self._uo_interps.append(
                RegularGridInterpolator(
                    (self._lats, self._lons), uo_filled[t_idx],
                    method="linear", bounds_error=False, fill_value=None,
                )
            )
            self._vo_interps.append(
                RegularGridInterpolator(
                    (self._lats, self._lons), vo_filled[t_idx],
                    method="linear", bounds_error=False, fill_value=None,
                )
            )

        ds.close()

    @staticmethod
    def _fill_nans(data: np.ndarray) -> np.ndarray:
        """
        Replace NaN cells with the nearest valid cell value using a simple
        iterative nearest-neighbor fill per 2D slice.
        If an entire slice is NaN, leaves NaN (caller gets OutOfDomainError).
        """
        out = data.copy()
        for t_idx in range(out.shape[0]):
            slc = out[t_idx]
            nan_mask = np.isnan(slc)
            if not nan_mask.any():
                continue
            if nan_mask.all():
                continue  # entire slice NaN — nothing to fill from

            # Nearest-neighbor fill: for each NaN cell, find the nearest
            # valid cell by row/col distance and use its value.
            valid_idx = np.argwhere(~nan_mask)  # shape (N, 2)
            nan_idx   = np.argwhere(nan_mask)   # shape (M, 2)
            for ni in nan_idx:
                diffs = valid_idx - ni
                dists = np.sum(diffs ** 2, axis=1)
                nearest = valid_idx[np.argmin(dists)]
                slc[ni[0], ni[1]] = slc[nearest[0], nearest[1]]
            out[t_idx] = slc
        return out

    # ─────────────────────────────────────────────────────────────
    # Core query
    # ─────────────────────────────────────────────────────────────

    def query(
        self,
        lat: float,
        lon: float,
        t: datetime,
    ) -> Tuple[float, float]:
        """
        Query the current velocity at (lat, lon, t) via bilinear spatial
        interpolation and linear temporal interpolation between daily means.

        Parameters
        ----------
        lat, lon : float
            WGS-84 decimal degrees. Must be inside the downloaded grid domain.
        t : datetime
            UTC-aware datetime. Must be within the dataset's time coverage.

        Returns
        -------
        (uo, vo) : (float, float)
            Eastward and northward current components in m/s.
            uo > 0 = flowing east; vo > 0 = flowing north.
            These are velocity VECTOR COMPONENTS, not direction angles.

        Raises
        ------
        OutOfDomainError
            If (lat, lon) is outside the grid bounds.
        OutOfTimeRangeError
            If t is outside the temporal coverage.
        ValueError
            If t is timezone-naive.
        """
        if t.tzinfo is None:
            raise ValueError("Datetime t must be timezone-aware (UTC)")

        t_utc = t.astimezone(timezone.utc)
        ts = t_utc.timestamp()

        # Domain check
        if lat < self._lat_min or lat > self._lat_max:
            raise OutOfDomainError(
                f"lat={lat} outside dataset range [{self._lat_min:.3f}, {self._lat_max:.3f}]"
            )
        if lon < self._lon_min or lon > self._lon_max:
            raise OutOfDomainError(
                f"lon={lon} outside dataset range [{self._lon_min:.3f}, {self._lon_max:.3f}]"
            )

        # Time check — allow small extrapolation (half a day) past dataset edges
        half_day = 43200.0
        if ts < self._times_ts[0] - half_day or ts > self._times_ts[-1] + half_day:
            raise OutOfTimeRangeError(
                f"t={t_utc.isoformat()} outside dataset range "
                f"[{self._times_dt[0].isoformat()}, {self._times_dt[-1].isoformat()}]"
            )

        # Spatial interpolation at each bracketing time step
        point = np.array([[lat, lon]])

        # Find bracketing time indices
        idx_hi = int(np.searchsorted(self._times_ts, ts))
        idx_hi = min(idx_hi, len(self._times_ts) - 1)
        idx_lo = max(idx_hi - 1, 0)

        uo_lo = float(self._uo_interps[idx_lo](point)[0])
        vo_lo = float(self._vo_interps[idx_lo](point)[0])

        if idx_lo == idx_hi:
            return uo_lo, vo_lo

        uo_hi = float(self._uo_interps[idx_hi](point)[0])
        vo_hi = float(self._vo_interps[idx_hi](point)[0])

        # Handle interpolators returning NaN (shouldn't happen after fill)
        if not math.isfinite(uo_lo): uo_lo = uo_hi
        if not math.isfinite(vo_lo): vo_lo = vo_hi
        if not math.isfinite(uo_hi): uo_hi = uo_lo
        if not math.isfinite(vo_hi): vo_hi = vo_lo

        # Linear temporal interpolation
        t_lo, t_hi = self._times_ts[idx_lo], self._times_ts[idx_hi]
        span = t_hi - t_lo
        frac = 0.0 if span <= 0 else (ts - t_lo) / span
        frac = max(0.0, min(1.0, frac))

        uo = uo_lo + (uo_hi - uo_lo) * frac
        vo = vo_lo + (vo_hi - vo_lo) * frac
        return float(uo), float(vo)

    # ─────────────────────────────────────────────────────────────
    # Forcing series builder
    # ─────────────────────────────────────────────────────────────

    def build_current_series(
        self,
        lat: float,
        lon: float,
        t_start: datetime,
        t_end: datetime,
        dt_hours: float = 1.0,
    ) -> List[Dict[str, Any]]:
        """
        Build a list of current-only forcing dicts at regular hourly intervals
        between t_start and t_end.

        TEMPORAL RESOLUTION NOTE:
            The underlying data is DAILY MEAN. Values between daily midpoints
            are linearly interpolated — this does NOT create new physical
            information. Use with documented awareness of this limitation.

        Parameters
        ----------
        lat, lon : float
        t_start, t_end : datetime (UTC-aware)
        dt_hours : float
            Output time step in hours (default 1 h).

        Returns
        -------
        List of dicts with keys:
            t                  : datetime (UTC-aware)
            current_speed_ms   : float (m/s)
            current_dir_deg    : float (degrees — direction current is flowing TOWARD)
            current_uo_ms      : float (eastward component)
            current_vo_ms      : float (northward component)
            current_source     : str
        """
        if t_start.tzinfo is None or t_end.tzinfo is None:
            raise ValueError("t_start and t_end must be timezone-aware UTC datetimes")

        dt_s = int(dt_hours * 3600)
        result = []
        t_cur = t_start.astimezone(timezone.utc)
        t_end_utc = t_end.astimezone(timezone.utc)

        while t_cur <= t_end_utc:
            uo, vo = self.query(lat, lon, t_cur)
            speed = math.sqrt(uo ** 2 + vo ** 2)
            # Direction the current is flowing TOWARD (0° = north, clockwise)
            direction_toward = math.degrees(math.atan2(uo, vo)) % 360.0
            result.append({
                "t":                 t_cur,
                "current_speed_ms":  speed,
                "current_dir_deg":   direction_toward,   # TOWARD convention
                "current_uo_ms":     uo,
                "current_vo_ms":     vo,
                "current_source":    "CMEMS_GLORYS12V1_daily_mean",
            })
            t_cur = t_cur + timedelta(seconds=dt_s)

        return result

    # ─────────────────────────────────────────────────────────────
    # Merge with wind to produce combined forcing series
    # ─────────────────────────────────────────────────────────────

    @property
    def lat_range(self) -> Tuple[float, float]:
        return (float(self._lats.min()), float(self._lats.max()))

    @property
    def lon_range(self) -> Tuple[float, float]:
        return (float(self._lons.min()), float(self._lons.max()))

    @property
    def time_range(self) -> Tuple[datetime, datetime]:
        return (self._times_dt[0], self._times_dt[-1])

    @property
    def n_time_steps(self) -> int:
        return len(self._times_dt)


# ─────────────────────────────────────────────────────────────────
# merge_wind_and_cmems_current
# ─────────────────────────────────────────────────────────────────

def merge_wind_and_cmems_current(
    wind_series: List[Dict[str, Any]],
    cmems_loader: CmemsCurrentLoader,
    lat: float,
    lon: float,
    current_dir_convention: str = "toward",
) -> List[Dict[str, Any]]:
    """
    Merge an Open-Meteo wind series (from forcing_loader.py) with CMEMS
    historical ocean currents to produce a combined forcing series compatible
    with simulate_ensemble() and backward_propagate().

    DIRECTION CONVENTION:
    The existing forcing_loader.py returns current_dir_deg in FROM convention
    (after converting Open-Meteo TOWARD → FROM). CMEMS uo/vo are vector
    components. This function converts CMEMS uo/vo to speed + TOWARD direction,
    then either:
      - current_dir_convention="toward": stores as-is (pipeline will apply
        TOWARD→FROM conversion if configured in forcing_loader.py)
      - current_dir_convention="from": converts TOWARD to FROM here
        (adds 180°) — matches the existing pipeline default

    The return dict matches the forcing_series schema of simulate_ensemble():
        {t, wind_speed_ms, wind_dir_deg, current_speed_ms, current_dir_deg}

    Parameters
    ----------
    wind_series : List[Dict]
        Output of load_historical_forcing() (wind only, current may be absent
        or from Open-Meteo which was all-nulls for Arabian Sea).
    cmems_loader : CmemsCurrentLoader
    lat, lon : float
        Location for current query.
    current_dir_convention : str
        "from" (default for compatibility) or "toward".

    Returns
    -------
    List[Dict] — merged forcing series, sorted by t ascending.
    """
    # Build a lookup map from timestamp to wind row
    wind_map = {r["t"]: r for r in wind_series}

    merged = []
    for wind_row in wind_series:
        t = wind_row["t"]
        try:
            uo, vo = cmems_loader.query(lat, lon, t)
        except (OutOfDomainError, OutOfTimeRangeError):
            # Outside CMEMS coverage — skip this timestamp
            continue

        speed = math.sqrt(uo ** 2 + vo ** 2)
        dir_toward = math.degrees(math.atan2(uo, vo)) % 360.0

        if current_dir_convention == "from":
            current_dir = (dir_toward + 180.0) % 360.0
        else:
            current_dir = dir_toward

        merged.append({
            "t":                wind_row["t"],
            "wind_speed_ms":    wind_row["wind_speed_ms"],
            "wind_dir_deg":     wind_row["wind_dir_deg"],
            "current_speed_ms": speed,
            "current_dir_deg":  current_dir,
            # Provenance fields (ignored by physics, useful for debugging)
            "wind_source":      wind_row.get("wind_source", "open_meteo_archive"),
            "current_source":   "CMEMS_GLORYS12V1_daily_mean",
            "current_uo_ms":    uo,
            "current_vo_ms":    vo,
        })

    return sorted(merged, key=lambda r: r["t"])
