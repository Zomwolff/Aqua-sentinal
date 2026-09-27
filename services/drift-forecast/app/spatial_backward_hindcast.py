"""
spatial_backward_hindcast.py
=============================
Spatially varying backward advection ensemble for Aqua-Sentinel.

WHAT THIS MODULE DOES:
    Provides backward_propagate_spatial() — a drop-in replacement for
    backward_propagate() that evaluates environmental forcing at each
    particle's current geographic position rather than at a single point.

WHAT THIS MODULE DOES NOT DO:
    - Does NOT modify backward_hindcast.py
    - Does NOT modify simulate_ensemble() or particles.py
    - Does NOT change the backward algorithm (same time-reversed displacement)
    - Does NOT change diffusion handling (still excluded, same as baseline)
    - Does NOT change windage parameterization

CHANGE FROM BASELINE:
    backward_propagate():    forcing = _interp_forcing(series, t)  [one value]
    backward_propagate_spatial(): forcing = field.query_array(t, lat_p, lon_p)  [per particle]

ALL OTHER PHYSICS IDENTICAL:
    - Windage range: (0.01, 0.04)
    - Windage resampling: WINDAGE_RESAMPLE_INTERVAL_S
    - Integration timestep: INTEGRATION_STEP_S
    - Displacement sign: SUBTRACT (backward)
    - No diffusion (irreversible, excluded same as baseline)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from app.particles import (
    INTEGRATION_STEP_S,
    MIN_RADIUS_M,
    WINDAGE_RESAMPLE_INTERVAL_S,
)
from app.backward_hindcast import BackwardEnsembleResult, _rms_spread
from app.spatial_forcing_field import SpatialForcingField


def backward_propagate_spatial(
    observed_lat: float,
    observed_lon: float,
    detection_time: datetime,
    forcing_field: SpatialForcingField,
    horizon_hours: float = 96.0,
    n_particles: int = 300,
    seed: int = 42,
    windage_range: Tuple[float, float] = (0.01, 0.04),
    observed_area_m2: float = 1_000_000.0,
) -> BackwardEnsembleResult:
    """
    Backward advection ensemble with per-particle spatial forcing.

    Identical to backward_propagate() except:
    - forcing_field.query_array(t, lat_p, lon_p) is called each step
      to get per-particle (we, wn, ce, cn)
    - No single-point forcing_series required

    Parameters
    ----------
    observed_lat, observed_lon : float
        Observed spill centroid.
    detection_time : datetime (UTC-aware)
        Observation timestamp.
    forcing_field : SpatialForcingField
        Pre-built spatial wind + current field.
    horizon_hours : float
        Backward integration horizon. Default 96 h.
    n_particles : int
        Ensemble size. Default 300.
    seed : int
        RNG seed.
    windage_range : Tuple[float, float]
        Per-particle windage range. Same default as baseline.
    observed_area_m2 : float
        Initial particle spread area.

    Returns
    -------
    BackwardEnsembleResult — same type as backward_propagate().
    """
    if detection_time.tzinfo is None:
        raise ValueError("detection_time must be UTC-aware")
    detection_utc = detection_time.astimezone(timezone.utc)

    # ── Initialize particles ─────────────────────────────────────
    rng = np.random.default_rng(seed)
    base_radius = max(MIN_RADIUS_M, math.sqrt(observed_area_m2 / math.pi))
    m_lat = 111_319.0
    m_lon = m_lat * math.cos(math.radians(observed_lat)) + 1e-10

    angles = rng.uniform(0.0, 2.0 * math.pi, n_particles)
    radii  = base_radius * np.sqrt(rng.uniform(0.0, 1.0, n_particles))
    lat_p  = observed_lat + radii * np.sin(angles) / m_lat
    lon_p  = observed_lon + radii * np.cos(angles) / m_lon

    windage_coeff         = rng.uniform(windage_range[0], windage_range[1], n_particles)
    windage_resample_timer = np.zeros(n_particles)

    # ── Integration ───────────────────────────────────────────────
    dt = INTEGRATION_STEP_S
    total_steps = int(math.ceil(horizon_hours * 3600.0 / dt))
    trajectory_history: List[Dict[str, Any]] = []
    t_current = detection_utc

    for step in range(total_steps):
        # Per-particle forcing at current positions
        we_p, wn_p, ce_p, cn_p = forcing_field.query_array(t_current, lat_p, lon_p)

        # Resample windage periodically
        windage_resample_timer += dt
        resample_mask = windage_resample_timer >= WINDAGE_RESAMPLE_INTERVAL_S
        if resample_mask.any():
            windage_coeff[resample_mask] = rng.uniform(
                windage_range[0], windage_range[1], int(resample_mask.sum())
            )
            windage_resample_timer[resample_mask] = 0.0

        # Per-particle total velocity (current + windage * wind)
        ve = ce_p + windage_coeff * we_p
        vn = cn_p + windage_coeff * wn_p

        # Backward displacement — SUBTRACT (same sign as baseline)
        # m_lon varies with latitude; use per-particle latitude
        m_lon_p = m_lat * np.cos(np.radians(lat_p)) + 1e-10
        lat_p -= vn * dt / m_lat
        lon_p -= ve * dt / m_lon_p

        t_current = datetime.fromtimestamp(
            t_current.timestamp() - dt, tz=timezone.utc
        )

        centroid_lat = float(lat_p.mean())
        centroid_lon = float(lon_p.mean())
        spread_m = _rms_spread(lat_p, lon_p, centroid_lat, centroid_lon)
        trajectory_history.append({
            "t":            t_current,
            "lat":          lat_p.copy(),
            "lon":          lon_p.copy(),
            "centroid_lat": centroid_lat,
            "centroid_lon": centroid_lon,
            "spread_m":     spread_m,
        })

    final_lat = lat_p.copy()
    final_lon = lon_p.copy()
    final_centroid_lat = float(final_lat.mean())
    final_centroid_lon = float(final_lon.mean())

    return BackwardEnsembleResult(
        detection_time=detection_utc,
        horizon_hours=horizon_hours,
        n_particles=n_particles,
        trajectory_history=trajectory_history,
        final_particles={"lat": final_lat, "lon": final_lon},
        final_centroid=(final_centroid_lat, final_centroid_lon),
        uncertainty_metrics={
            "rms_spread_m": _rms_spread(final_lat, final_lon, final_centroid_lat, final_centroid_lon),
            "min_lat": float(final_lat.min()), "max_lat": float(final_lat.max()),
            "min_lon": float(final_lon.min()), "max_lon": float(final_lon.max()),
            "total_steps": total_steps, "dt_s": dt,
        },
    )
