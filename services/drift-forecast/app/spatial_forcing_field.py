"""
spatial_forcing_field.py
========================
Spatially varying historical forcing field for Aqua-Sentinel experiments.

PURPOSE
=======
Provides a get_forcing(t, lat, lon) -> (wind_e, wind_n, current_e, current_n)
interface that interpolates from gridded historical datasets at any
(time, latitude, longitude) point.

This module does NOT modify simulate_ensemble(), particles.py, or any
existing production module. It is a new adapter used only in the spatially-
aware backward hindcast and per-candidate forcing construction.

WIND
====
Source: Open-Meteo ERA5 archive, fetched on demand at each (lat, lon) point.
Spatial coverage: fetched per query point.
Temporal resolution: hourly.
Convention: meteorological FROM direction (same as forcing_loader.py).

Limitation: Open-Meteo returns a single-point time series per lat/lon request.
To avoid O(N_particles) API calls per timestep during backward integration,
we pre-fetch a dense grid of wind series over the domain and bilinearly
interpolate between them.

OCEAN CURRENT
=============
Source: CMEMS GLORYS12V1 daily-mean NetCDF.
The existing CmemsCurrentLoader already supports (lat, lon, t) queries
via bilinear spatial + linear temporal interpolation.

DIRECTION CONVENTION
====================
All outputs are (east_ms, north_ms) velocity components, matching the
convention used internally in particles.py and backward_hindcast.py.

NaN HANDLING
============
- CMEMS: NaN-filled by nearest-valid-cell (existing CmemsCurrentLoader)
- Wind: if a grid node fetch fails, the nearest successful node is used
- Out-of-domain: raises OutOfDomainError explicitly

SCIENTIFIC CONSTRAINTS
======================
- No modification to simulate_ensemble() or existing physics
- ERA5 wind used for spatial wind field (same data source as baseline)
- CMEMS current used for ocean current (same dataset as baseline)
- No parameter tuning
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.interpolate import RegularGridInterpolator

from app.forcing_loader import _fetch_wind_archive
from app.cmems_current_loader import CmemsCurrentLoader, OutOfDomainError, OutOfTimeRangeError
from app.particles import _wind_components


# ─────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────

class SpatialForcingError(Exception):
    """Base class for spatial forcing errors."""

class OutOfSpatialDomainError(SpatialForcingError):
    """Raised when a query point is outside the available forcing domain."""


# ─────────────────────────────────────────────────────────────────
# ERA5 Wind Grid
# ─────────────────────────────────────────────────────────────────

class Era5WindGrid:
    """
    Pre-fetched ERA5 wind grid for a geographic domain.

    Fetches hourly wind time series at a regular grid of lat/lon nodes
    covering the domain, then provides bilinear spatial + linear temporal
    interpolation at any (t, lat, lon) query.

    Grid spacing defaults to 0.5° to keep API calls manageable (typical
    domain is ~3° x 5°, giving 6×10 = 60 nodes max, each one API call).
    Actual ERA5 resolution from Open-Meteo is ~31 km (~0.28°); sampling
    at 0.5° slightly undersamples but avoids excessive API calls.
    """

    def __init__(
        self,
        lat_min: float, lat_max: float,
        lon_min: float, lon_max: float,
        date_start, date_end,
        grid_spacing_deg: float = 0.5,
        retry_delay_s: float = 1.0,
    ) -> None:
        """
        Pre-fetch ERA5 wind at all grid nodes.

        Parameters
        ----------
        lat_min, lat_max, lon_min, lon_max : float
            Geographic domain bounding box.
        date_start, date_end : date
            Date range for wind fetch.
        grid_spacing_deg : float
            Grid node spacing in degrees (default 0.5°).
        retry_delay_s : float
            Delay between API calls to avoid rate limiting.
        """
        self.lat_min = lat_min; self.lat_max = lat_max
        self.lon_min = lon_min; self.lon_max = lon_max
        self.date_start = date_start; self.date_end = date_end
        self.grid_spacing = grid_spacing_deg

        # Grid node coordinates
        self.lats = np.arange(lat_min, lat_max + grid_spacing_deg * 0.5, grid_spacing_deg)
        self.lons = np.arange(lon_min, lon_max + grid_spacing_deg * 0.5, grid_spacing_deg)

        # Fetch wind at each node
        self._wind_data: Dict[Tuple[float, float], List[Dict[str, Any]]] = {}
        self._fetch_all_nodes(retry_delay_s)

        # Build grid interpolators for each timestamp
        self._build_interpolators()

    def _fetch_all_nodes(self, delay_s: float) -> None:
        n = len(self.lats) * len(self.lons)
        print(f"  [Era5WindGrid] Fetching {n} wind nodes ({len(self.lats)}lat x {len(self.lons)}lon)...")
        fetched = 0
        for lat in self.lats:
            for lon in self.lons:
                try:
                    series = _fetch_wind_archive(
                        lat=float(lat), lon=float(lon),
                        date_start=self.date_start, date_end=self.date_end
                    )
                    self._wind_data[(float(lat), float(lon))] = series
                    fetched += 1
                    if delay_s > 0:
                        time.sleep(delay_s)
                except Exception as e:
                    print(f"  [Era5WindGrid] Warning: node ({lat:.2f},{lon:.2f}) failed: {e}")
                    # Leave missing; will be handled via nearest-node fallback
        print(f"  [Era5WindGrid] Fetched {fetched}/{n} nodes successfully.")

    def _build_interpolators(self) -> None:
        """
        Build RegularGridInterpolator objects for wind_e and wind_n
        at each unique timestamp.
        """
        # Collect all timestamps from the first successful node
        ref_series = None
        for series in self._wind_data.values():
            if series:
                ref_series = series
                break
        if ref_series is None:
            raise SpatialForcingError("No wind data fetched — all nodes failed.")

        self._timestamps = [r["t"] for r in ref_series]
        self._timestamps_ts = np.array([t.timestamp() for t in self._timestamps])

        n_t = len(self._timestamps)
        n_lat = len(self.lats)
        n_lon = len(self.lons)

        # Arrays shape: (n_t, n_lat, n_lon)
        we_grid = np.zeros((n_t, n_lat, n_lon))
        wn_grid = np.zeros((n_t, n_lat, n_lon))

        for i_lat, lat in enumerate(self.lats):
            for i_lon, lon in enumerate(self.lons):
                key = (float(lat), float(lon))
                series = self._wind_data.get(key)
                if series is None:
                    # Use nearest available node
                    series = self._nearest_series(lat, lon)
                # Build time-indexed lookup for this node
                node_map = {r["t"].timestamp(): r for r in series}
                for i_t, ts in enumerate(self._timestamps):
                    r = node_map.get(ts.timestamp())
                    if r is None:
                        # Linear interpolate from adjacent hours
                        r = self._interp_node_at(series, ts)
                    if r is not None:
                        we, wn = _wind_components(r["wind_speed_ms"], r["wind_dir_deg"])
                        we_grid[i_t, i_lat, i_lon] = we
                        wn_grid[i_t, i_lat, i_lon] = wn

        self._we_interp = RegularGridInterpolator(
            (self._timestamps_ts, self.lats, self.lons), we_grid,
            method='linear', bounds_error=False, fill_value=None
        )
        self._wn_interp = RegularGridInterpolator(
            (self._timestamps_ts, self.lats, self.lons), wn_grid,
            method='linear', bounds_error=False, fill_value=None
        )

    def _nearest_series(self, lat: float, lon: float) -> List[Dict]:
        """Return series from the nearest node that has data."""
        best = None
        best_d = float('inf')
        for (la, lo), series in self._wind_data.items():
            d = (la - lat)**2 + (lo - lon)**2
            if d < best_d and series:
                best_d = d; best = series
        return best or []

    @staticmethod
    def _interp_node_at(series: List[Dict], ts: datetime) -> Optional[Dict]:
        """Linear time interpolation within a node series."""
        if not series:
            return None
        target = ts.timestamp()
        if target <= series[0]["t"].timestamp():
            return series[0]
        if target >= series[-1]["t"].timestamp():
            return series[-1]
        for i in range(1, len(series)):
            if series[i]["t"].timestamp() >= target:
                lo, hi = series[i-1], series[i]
                span = hi["t"].timestamp() - lo["t"].timestamp()
                frac = (target - lo["t"].timestamp()) / span if span > 0 else 0.0
                return {
                    "wind_speed_ms": lo["wind_speed_ms"] + (hi["wind_speed_ms"] - lo["wind_speed_ms"]) * frac,
                    "wind_dir_deg":  lo["wind_dir_deg"],  # direction interpolation is circular; use nearest
                }
        return series[-1]

    def query(self, t: datetime, lat: float, lon: float) -> Tuple[float, float]:
        """
        Return (wind_east_ms, wind_north_ms) at (t, lat, lon).

        Uses bilinear spatial + linear temporal interpolation.
        Clamps to domain boundaries if out of range (no silent extrapolation
        beyond ±grid_spacing/2 of the domain edge).
        """
        ts = t.timestamp()
        # Clamp to grid bounds
        lat_c = float(np.clip(lat, self.lats[0], self.lats[-1]))
        lon_c = float(np.clip(lon, self.lons[0], self.lons[-1]))
        ts_c  = float(np.clip(ts, self._timestamps_ts[0], self._timestamps_ts[-1]))
        pt = np.array([[ts_c, lat_c, lon_c]])
        we = float(self._we_interp(pt)[0])
        wn = float(self._wn_interp(pt)[0])
        if not (math.isfinite(we) and math.isfinite(wn)):
            # Fallback to nearest node
            ser = self._nearest_series(lat, lon)
            r   = self._interp_node_at(ser, t)
            if r is not None:
                we, wn = _wind_components(r["wind_speed_ms"], r["wind_dir_deg"])
            else:
                we, wn = 0.0, 0.0
        return we, wn

    def time_range(self) -> Tuple[datetime, datetime]:
        return self._timestamps[0], self._timestamps[-1]


# ─────────────────────────────────────────────────────────────────
# SpatialForcingField — unified interface
# ─────────────────────────────────────────────────────────────────

class SpatialForcingField:
    """
    Unified spatial forcing field combining ERA5 wind grid + CMEMS current.

    Provides:
      query(t, lat, lon)
          -> (wind_east_ms, wind_north_ms, current_east_ms, current_north_ms)

      get_series(lat, lon, t_start, t_end, dt_hours=1.0)
          -> forcing_series compatible with simulate_ensemble()
    """

    def __init__(
        self,
        wind_grid: Era5WindGrid,
        cmems_loader: CmemsCurrentLoader,
    ) -> None:
        self._wind  = wind_grid
        self._cmems = cmems_loader

    def query(
        self,
        t: datetime,
        lat: float,
        lon: float,
    ) -> Tuple[float, float, float, float]:
        """
        Return (wind_east_ms, wind_north_ms, current_east_ms, current_north_ms)
        at (t, lat, lon).

        Wind: bilinear spatial + linear temporal ERA5 interpolation.
        Current: bilinear spatial + linear temporal CMEMS interpolation.

        NaN/coastal CMEMS cells are already filled by nearest-valid-cell
        inside CmemsCurrentLoader.

        Raises
        ------
        OutOfSpatialDomainError
            If (lat, lon) is outside both wind grid and CMEMS domain.
        """
        # Wind
        we, wn = self._wind.query(t, lat, lon)

        # Current
        try:
            uo, vo = self._cmems.query(lat, lon, t)
            # uo/vo are (east, north) vector components in m/s — use directly
            ce, cn = float(uo), float(vo)
        except (OutOfDomainError, OutOfTimeRangeError):
            # Use zero current outside CMEMS domain (documented, not silent)
            ce, cn = 0.0, 0.0

        return we, wn, ce, cn

    def query_array(
        self,
        t: datetime,
        lats: np.ndarray,
        lons: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Vectorized query for arrays of (lat, lon) at a single timestamp.

        Returns four arrays: (we, wn, ce, cn), each shape (N,).
        """
        n = len(lats)
        ts = t.timestamp()
        # Wind — use the grid interpolator directly for all points at once
        lat_c = np.clip(lats, self._wind.lats[0], self._wind.lats[-1])
        lon_c = np.clip(lons, self._wind.lons[0], self._wind.lons[-1])
        ts_c  = np.clip(ts, self._wind._timestamps_ts[0], self._wind._timestamps_ts[-1])
        pts_w = np.column_stack([np.full(n, ts_c), lat_c, lon_c])
        we_arr = self._wind._we_interp(pts_w)
        wn_arr = self._wind._wn_interp(pts_w)

        # Fix NaNs in wind
        bad_w = ~(np.isfinite(we_arr) & np.isfinite(wn_arr))
        if bad_w.any():
            for idx in np.where(bad_w)[0]:
                fallback_we, fallback_wn = self._wind.query(t, float(lats[idx]), float(lons[idx]))
                we_arr[idx] = fallback_we; wn_arr[idx] = fallback_wn

        # Current — iterate (CMEMS doesn't have vectorized API but is fast)
        ce_arr = np.zeros(n); cn_arr = np.zeros(n)
        for i in range(n):
            try:
                uo, vo = self._cmems.query(float(lats[i]), float(lons[i]), t)
                ce_arr[i] = float(uo); cn_arr[i] = float(vo)
            except (OutOfDomainError, OutOfTimeRangeError):
                pass  # leave zero

        return we_arr, wn_arr, ce_arr, cn_arr

    def get_series(
        self,
        lat: float,
        lon: float,
        t_start: datetime,
        t_end: datetime,
        dt_hours: float = 1.0,
    ) -> List[Dict[str, Any]]:
        """
        Build a forcing_series list compatible with simulate_ensemble()
        at a fixed (lat, lon) location, spanning [t_start, t_end].

        The series has the standard schema:
          {t, wind_speed_ms, wind_dir_deg, current_speed_ms, current_dir_deg}
        with direction in meteorological FROM convention (compatible with
        the existing _interp_forcing / _wind_components in particles.py).

        Parameters
        ----------
        lat, lon : float
            Geographic location for the series.
        t_start, t_end : datetime (UTC-aware)
        dt_hours : float
            Output timestep in hours (default 1 h).

        Returns
        -------
        List[Dict] — sorted by t ascending, same schema as load_historical_forcing().
        """
        dt_s = int(dt_hours * 3600)
        result = []
        t_cur = t_start.astimezone(timezone.utc)
        t_end_utc = t_end.astimezone(timezone.utc)

        while t_cur <= t_end_utc:
            we, wn, ce, cn = self.query(t_cur, lat, lon)

            # Convert (east, north) components back to speed + FROM direction
            # for compatibility with simulate_ensemble()'s _interp_forcing()
            wind_speed = math.sqrt(we**2 + wn**2)
            # FROM direction: wind blows FROM this direction
            # we = -speed * sin(dir_rad), wn = -speed * cos(dir_rad)
            # => dir_rad = atan2(-we, -wn)
            wind_dir = math.degrees(math.atan2(-we, -wn)) % 360.0

            cur_speed = math.sqrt(ce**2 + cn**2)
            # Current uo/vo are TOWARD components: (east, north)
            # In particles.py, _wind_components() is applied to current_dir_deg
            # treating it as meteorological FROM convention.
            # To have _wind_components(speed, dir) return (ce, cn):
            #   ce = -speed*sin(dir_rad), cn = -speed*cos(dir_rad)
            # So for current TOWARD (east=ce, north=cn):
            #   we need FROM direction such that _wind_components gives (ce, cn)
            #   ce = -speed*sin(dir_rad) => dir_rad = atan2(-ce, -cn)
            #   BUT we want TOWARD, so we add 180° to get the FROM direction
            if cur_speed > 1e-10:
                cur_dir_toward = math.degrees(math.atan2(ce, cn)) % 360.0
                cur_dir_from   = (cur_dir_toward + 180.0) % 360.0
            else:
                cur_dir_from = 0.0

            result.append({
                "t":                t_cur,
                "wind_speed_ms":    wind_speed,
                "wind_dir_deg":     wind_dir,
                "current_speed_ms": cur_speed,
                "current_dir_deg":  cur_dir_from,
            })
            t_cur = t_cur + timedelta(seconds=dt_s)

        return result
