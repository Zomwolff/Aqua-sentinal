"""
HYCOM Current Loader — hycom_current_loader.py

PURPOSE
=======
Loads HYCOM GLBy0.08 Experiment 93.0 3-hourly ocean-current reanalysis from a
local NetCDF subset file and returns current forcing in the format consumed by
the Aqua-Sentinel spatial drift forecast pipeline for Wakashio Experiment C.

DATASET
=======
Product:  HYCOM GLBy0.08 Experiment 93.0
Source:   HYCOM Consortium / Naval Research Laboratory
Variables: water_u (eastward, m/s), water_v (northward, m/s)
Depth:    0.0 m (surface level)
Grid:     0.08° (~9 km), regular lat/lon
Temporal: 3-hourly (8 timesteps per day)
Coverage: (for Wakashio subset) 2020-08-04 to 2020-08-12,
          lat -22° to -18°, lon 55.5° to 60°

DIRECTION CONVENTION — CRITICAL
================================
water_u and water_v are velocity VECTOR COMPONENTS, not directional angles.
    water_u > 0  →  water flowing eastward
    water_v > 0  →  water flowing northward

These are NOT meteorological FROM-directions. They must NOT be converted
using _wind_components() from particles.py. They are used directly as
east/north velocity components in the spatial forcing field.

TEMPORAL RESOLUTION
===================
This dataset provides 3-hourly ocean currents (8 timesteps per day).
The particle integration step is 600 s (10 minutes). Linear interpolation
between 3-hourly fields is applied to produce sub-hourly forcing at query times.

This provides significantly higher temporal resolution than daily-mean products
(CMEMS GLORYS12V1), allowing capture of diurnal and semi-diurnal variability
in ocean currents. This temporal resolution difference is the controlled
experimental variable in Wakashio Experiment C.

SPATIAL INTERPOLATION
======================
Bilinear interpolation on the 0.08° HYCOM grid using scipy.interpolate.RegularGridInterpolator.
NaN cells (land/coastal masking) are filled by nearest-valid-cell before
interpolation. If all surrounding cells are NaN, the query raises OutOfDomainError.

WHAT THIS MODULE DOES NOT DO
=============================
- Does NOT modify simulate_ensemble() or any existing physics
- Does NOT replace the existing wind loader (Era5WindGrid)
- Does NOT produce wind data — only ocean current
- The caller (SpatialForcingField) is responsible for combining wind and
  current into a merged forcing query interface
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Tuple

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

class HycomCurrentError(Exception):
    """Base exception for HYCOM current loader."""


class OutOfDomainError(HycomCurrentError):
    """Raised when query lat/lon falls outside the downloaded grid extent."""


class OutOfTimeRangeError(HycomCurrentError):
    """Raised when query time is outside the dataset's temporal coverage."""


class NcFileNotFoundError(HycomCurrentError):
    """Raised when the NetCDF file does not exist."""


# ─────────────────────────────────────────────────────────────────
# HycomCurrentLoader
# ─────────────────────────────────────────────────────────────────

class HycomCurrentLoader:
    """
    Loads HYCOM GLBy0.08 3-hourly ocean currents and provides interpolated
    (lat, lon, t) → (current_east_ms, current_north_ms) queries.

    Usage:
        loader = HycomCurrentLoader("validation-data/hycom_wakashio_subset.nc")
        uo, vo = loader.query(lat=-20.40, lon=57.72,
                              t=datetime(2020, 8, 7, 12, 30, tzinfo=timezone.utc))

    This loader is designed for integration with SpatialForcingField in the
    Wakashio Experiment C validation pipeline.
    """

    def __init__(self, nc_path: str | Path) -> None:
        """
        Load and cache the HYCOM NetCDF subset.

        Parameters
        ----------
        nc_path : str or Path
            Path to the downloaded hycom_wakashio_subset.nc (or equivalent).

        Raises
        ------
        NcFileNotFoundError
            If the file does not exist.
        ImportError
            If xarray or scipy are not installed.
        ValueError
            If required variables (water_u, water_v) or coordinates are missing.
        """
        if not _XARRAY_AVAILABLE:
            raise ImportError(
                "xarray and scipy are required for HycomCurrentLoader. "
                "Install with: pip install xarray scipy netCDF4"
            )

        nc_path = Path(nc_path)
        if not nc_path.exists():
            raise NcFileNotFoundError(f"NetCDF file not found: {nc_path}")

        self._nc_path = nc_path
        self._load()

    def _load(self) -> None:
        """
        Parse the NetCDF and build bilinear interpolators per 3-hourly timestep.
        
        Steps:
        1. Open dataset with xarray
        2. Extract water_u, water_v at depth=0.0m (surface)
        3. Parse time coordinate (hours since 2000-01-01)
        4. Convert to UTC-aware datetime list
        5. Fill NaN land mask cells using _fill_nans()
        6. Build RegularGridInterpolator per timestep
        """
        ds = xr.open_dataset(self._nc_path)

        # Verify required variables exist
        if "water_u" not in ds or "water_v" not in ds:
            raise ValueError(
                f"NetCDF file missing required variables water_u and/or water_v. "
                f"Available variables: {list(ds.data_vars)}"
            )

        # Extract surface level (depth=0.0m) currents
        # HYCOM may have depth dimension - squeeze if single level, select if multiple
        if "depth" in ds.dims:
            # Select surface level (0.0 m)
            water_u_data = ds["water_u"].sel(depth=0.0, method="nearest").values
            water_v_data = ds["water_v"].sel(depth=0.0, method="nearest").values
        else:
            # No depth dimension - assume surface data
            water_u_data = ds["water_u"].values
            water_v_data = ds["water_v"].values

        # Ensure 3D shape (time, lat, lon)
        if water_u_data.ndim == 4:
            # Shape is (time, depth, lat, lon) - squeeze depth
            water_u_data = water_u_data.squeeze(axis=1)
            water_v_data = water_v_data.squeeze(axis=1)

        # Grid axes
        # HYCOM typically uses 'lat', 'lon' or 'latitude', 'longitude'
        lat_coord = "lat" if "lat" in ds.coords else "latitude"
        lon_coord = "lon" if "lon" in ds.coords else "longitude"
        
        self._lats = ds[lat_coord].values.astype(float)
        self._lons = ds[lon_coord].values.astype(float)

        # Parse times to UTC-aware datetimes
        # HYCOM time is typically "hours since 2000-01-01 00:00:00"
        time_var = ds["time"]
        
        # Convert to datetime using xarray's built-in datetime conversion
        if hasattr(time_var, 'to_numpy'):
            raw_times = time_var.to_numpy()
        else:
            raw_times = time_var.values
        
        # Handle different time formats
        if np.issubdtype(raw_times.dtype, np.datetime64):
            # Already datetime64
            self._times_dt = [
                datetime.utcfromtimestamp(t.astype('datetime64[s]').astype(int)).replace(tzinfo=timezone.utc)
                for t in raw_times
            ]
        else:
            # Numeric time - parse using units attribute
            time_units = time_var.attrs.get("units", "")
            if "hours since 2000-01-01" in time_units:
                # Parse manually from hours since 2000-01-01
                reference = datetime(2000, 1, 1, tzinfo=timezone.utc)
                self._times_dt = [
                    reference + timedelta(hours=float(h))
                    for h in raw_times
                ]
            else:
                # Try xarray's automatic decoding
                ds_decoded = xr.open_dataset(self._nc_path, decode_times=True)
                decoded_times = ds_decoded["time"].values
                self._times_dt = [
                    datetime.utcfromtimestamp(t.astype('datetime64[s]').astype(int)).replace(tzinfo=timezone.utc)
                    for t in decoded_times
                ]
                ds_decoded.close()

        self._times_ts = np.array([t.timestamp() for t in self._times_dt])

        # Domain bounds (with small tolerance for boundary queries)
        self._lat_min = float(self._lats.min()) - 0.01
        self._lat_max = float(self._lats.max()) + 0.01
        self._lon_min = float(self._lons.min()) - 0.01
        self._lon_max = float(self._lons.max()) + 0.01

        # Fill NaN (land/mask) cells using nearest valid
        water_u_filled = self._fill_nans(water_u_data.copy())
        water_v_filled = self._fill_nans(water_v_data.copy())

        # Build one RegularGridInterpolator per 3-hourly timestep
        # (bilinear in lat/lon — RegularGridInterpolator with 'linear' method)
        self._water_u_interps = []
        self._water_v_interps = []
        for t_idx in range(len(self._times_dt)):
            self._water_u_interps.append(
                RegularGridInterpolator(
                    (self._lats, self._lons), water_u_filled[t_idx],
                    method="linear", bounds_error=False, fill_value=None,
                )
            )
            self._water_v_interps.append(
                RegularGridInterpolator(
                    (self._lats, self._lons), water_v_filled[t_idx],
                    method="linear", bounds_error=False, fill_value=None,
                )
            )

        ds.close()

    @staticmethod
    def _fill_nans(data: np.ndarray) -> np.ndarray:
        """
        Replace NaN cells with the nearest valid cell value using Euclidean
        distance in (row, col) space.
        
        Strategy: For each NaN cell, find nearest non-NaN cell by
        Euclidean distance in (row, col) space and copy its value.
        
        This matches CmemsCurrentLoader's approach for consistency.
        
        If entire slice is NaN, leave as NaN (caller gets OutOfDomainError).
        
        Parameters
        ----------
        data : np.ndarray
            3D array with shape (time, lat, lon)
        
        Returns
        -------
        np.ndarray
            Array with NaN cells filled
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
            # valid cell by row/col Euclidean distance and use its value.
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
        interpolation and linear temporal interpolation between 3-hourly snapshots.

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
            If (lat, lon) is outside the grid bounds by more than 0.01°.
        OutOfTimeRangeError
            If t is outside the temporal coverage by more than 1 hour.
        ValueError
            If t is timezone-naive.
        """
        if t.tzinfo is None:
            raise ValueError("Datetime t must be timezone-aware (UTC)")

        t_utc = t.astimezone(timezone.utc)
        ts = t_utc.timestamp()

        # Domain check with 0.01° tolerance
        if lat < self._lat_min or lat > self._lat_max:
            raise OutOfDomainError(
                f"lat={lat} outside dataset range [{self._lat_min:.3f}, {self._lat_max:.3f}]"
            )
        if lon < self._lon_min or lon > self._lon_max:
            raise OutOfDomainError(
                f"lon={lon} outside dataset range [{self._lon_min:.3f}, {self._lon_max:.3f}]"
            )

        # Time check — allow 1 hour extrapolation past dataset edges
        one_hour = 3600.0
        if ts < self._times_ts[0] - one_hour or ts > self._times_ts[-1] + one_hour:
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

        water_u_lo = float(self._water_u_interps[idx_lo](point)[0])
        water_v_lo = float(self._water_v_interps[idx_lo](point)[0])

        # If query time exactly at a 3-hourly snapshot, return without temporal interpolation
        if idx_lo == idx_hi:
            return water_u_lo, water_v_lo

        water_u_hi = float(self._water_u_interps[idx_hi](point)[0])
        water_v_hi = float(self._water_v_interps[idx_hi](point)[0])

        # Handle interpolators returning NaN (shouldn't happen after fill, but check)
        if not math.isfinite(water_u_lo): water_u_lo = water_u_hi
        if not math.isfinite(water_v_lo): water_v_lo = water_v_hi
        if not math.isfinite(water_u_hi): water_u_hi = water_u_lo
        if not math.isfinite(water_v_hi): water_v_hi = water_v_lo

        # Verify all 4 surrounding cells aren't NaN after filling
        if not (math.isfinite(water_u_lo) and math.isfinite(water_v_lo) and 
                math.isfinite(water_u_hi) and math.isfinite(water_v_hi)):
            raise OutOfDomainError(
                f"All surrounding grid cells at ({lat}, {lon}) are NaN (land mask)"
            )

        # Linear temporal interpolation
        t_lo, t_hi = self._times_ts[idx_lo], self._times_ts[idx_hi]
        span = t_hi - t_lo
        frac = 0.0 if span <= 0 else (ts - t_lo) / span
        frac = max(0.0, min(1.0, frac))

        water_u = water_u_lo + (water_u_hi - water_u_lo) * frac
        water_v = water_v_lo + (water_v_hi - water_v_lo) * frac
        
        return float(water_u), float(water_v)

    # ─────────────────────────────────────────────────────────────
    # Properties
    # ─────────────────────────────────────────────────────────────

    @property
    def lat_range(self) -> Tuple[float, float]:
        """Return (min_lat, max_lat) coverage."""
        return (float(self._lats.min()), float(self._lats.max()))

    @property
    def lon_range(self) -> Tuple[float, float]:
        """Return (min_lon, max_lon) coverage."""
        return (float(self._lons.min()), float(self._lons.max()))

    @property
    def time_range(self) -> Tuple[datetime, datetime]:
        """Return (first_time, last_time) coverage."""
        return (self._times_dt[0], self._times_dt[-1])

    @property
    def n_time_steps(self) -> int:
        """Return number of 3-hourly timesteps."""
        return len(self._times_dt)
