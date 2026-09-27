"""
Step 3 Backward Advection Ensemble — backward_hindcast.py

WHAT THIS MODULE DOES:
    Implements the backward advection ensemble for the Aqua-Sentinel hindcasting
    pipeline (Step 3). Given an observed spill location and a window of historical
    environmental forcing, it advects a particle ensemble backward in time to
    generate an approximate origin envelope: the set of possible source locations
    consistent with the observed footprint under the recorded wind and current
    conditions.

SCIENTIFIC DISCLAIMER:
    This is an approximate backward advection envelope based on deterministic
    transport reversal. It does NOT account for diffusion, oil weathering, or
    spreading reversal. It is a search-space narrowing tool, not a definitive
    source attribution. Results should be interpreted as a probability envelope
    of possible past positions, not as exact backward trajectories.

PHYSICS INCLUDED:
    - Ocean current advection (reversed): lat -= cn*dt/m_lat
    - Wind-driven surface drift / windage (reversed): per-particle 1–4% of wind speed
    - Windage uncertainty: per-particle sampling from windage_range
    - Windage resampling: every WINDAGE_RESAMPLE_INTERVAL_S (900 s)

PHYSICS NOT REVERSED:
    - Fickian diffusion: irreversible; adding random noise backward is physically
      meaningless and is explicitly excluded
    - Fay spreading: non-reversible physical spreading process
    - Oil weathering (evaporation, emulsification): non-reversible
    - Wind deflection (Ekman): LEEWAY_DEFLECTION_ENABLED=false by default,
      same as the forward model

DIRECTION CONVENTION NOTE:
    forcing_series uses meteorological FROM convention for both wind_dir_deg and
    current_dir_deg (after forcing_loader.py applies its convention conversion).
    _interp_forcing() calls _wind_components() internally, which handles the
    FROM-direction → (east, north) m/s decomposition. No additional direction
    conversion is applied here. The backward engine only changes the sign of the
    displacement (subtract instead of add).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from app.particles import (
    _wind_components,
    _interp_forcing,
    INTEGRATION_STEP_S,
    MIN_RADIUS_M,
    WINDAGE_RESAMPLE_INTERVAL_S,
)


# ============================================================================
# BackwardEnsembleResult dataclass
# ============================================================================

@dataclass
class BackwardEnsembleResult:
    """
    Result of a backward advection ensemble run.

    SCIENTIFIC DISCLAIMER:
    This is an approximate backward advection envelope based on deterministic
    transport reversal. It does NOT account for diffusion, oil weathering, or
    spreading reversal. It is a search-space narrowing tool, not a definitive
    source attribution. Results should be interpreted as a probability envelope
    of possible past positions, not as exact backward trajectories.

    trajectory_history ordering:
        history[0]["t"] is the timestamp after the FIRST backward step
                        (≈ detection_time - dt)
        history[-1]["t"] is the timestamp after the LAST backward step
                        (≈ detection_time - horizon_hours)
    Timestamps are strictly DECREASING (detection → past).
    To get chronological (past → detection) order, reverse the list.
    """
    detection_time:      datetime
    horizon_hours:       float
    n_particles:         int
    trajectory_history:  List[Dict[str, Any]]
    final_particles:     Dict[str, Any]          # {"lat": np.ndarray, "lon": np.ndarray}
    final_centroid:      Tuple[float, float]     # (lat, lon)
    uncertainty_metrics: Dict[str, Any]


# ============================================================================
# Input validation
# ============================================================================

def _validate_backward_inputs(
    observed_lat: float,
    observed_lon: float,
    detection_time: datetime,
    forcing_series: Sequence[Dict[str, Any]],
    horizon_hours: float,
    n_particles: int,
    seed: int,
    windage_range: Tuple[float, float],
    observed_area_m2: float,
) -> datetime:
    """Validate all inputs. Returns detection_time normalized to UTC."""

    if detection_time.tzinfo is None:
        raise ValueError("detection_time must be timezone-aware UTC")

    detection_utc = detection_time.astimezone(timezone.utc)

    if observed_lat < -90.0 or observed_lat > 90.0:
        raise ValueError(
            f"observed_lat={observed_lat} is outside valid range [-90, 90]"
        )

    if observed_lon < -180.0 or observed_lon > 180.0:
        raise ValueError(
            f"observed_lon={observed_lon} is outside valid range [-180, 180]"
        )

    if horizon_hours <= 0:
        raise ValueError(
            f"horizon_hours={horizon_hours} must be positive (> 0)"
        )

    if n_particles < 1:
        raise ValueError(
            f"n_particles={n_particles} must be at least 1"
        )

    if seed < 0:
        raise ValueError(
            f"seed={seed} must be non-negative (>= 0)"
        )

    if (
        windage_range[0] < 0
        or windage_range[1] > 1
        or windage_range[0] > windage_range[1]
    ):
        raise ValueError(
            f"windage_range={windage_range} is invalid: values must satisfy "
            f"0 <= min <= max <= 1"
        )

    if observed_area_m2 <= 0:
        raise ValueError(
            f"observed_area_m2={observed_area_m2} must be positive (> 0)"
        )

    if len(forcing_series) < 2:
        raise ValueError("forcing_series must have at least 2 samples")

    return detection_utc


# ============================================================================
# Spread helper
# ============================================================================

def _rms_spread(
    lat_p: np.ndarray,
    lon_p: np.ndarray,
    centroid_lat: float,
    centroid_lon: float,
) -> float:
    """Vectorized RMS haversine distance from centroid (metres)."""
    R = 6_371_000.0
    phi1 = math.radians(centroid_lat)
    phi_p = np.radians(lat_p)
    dphi = np.radians(lat_p - centroid_lat)
    dlam = np.radians(lon_p - centroid_lon)
    a = np.sin(dphi / 2)**2 + math.cos(phi1) * np.cos(phi_p) * np.sin(dlam / 2)**2
    dist = 2.0 * R * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    return float(np.sqrt(np.mean(dist**2)))


# ============================================================================
# Main public function
# ============================================================================

def backward_propagate(
    observed_lat: float,
    observed_lon: float,
    detection_time: datetime,
    forcing_series: Sequence[Dict[str, Any]],
    horizon_hours: float = 24.0,
    n_particles: int = 500,
    seed: int = 42,
    windage_range: Tuple[float, float] = (0.01, 0.04),
    observed_area_m2: float = 1_000_000.0,
) -> BackwardEnsembleResult:
    """
    Backward advection ensemble — integrate particles from detection_time
    backward by horizon_hours using historical forcing.

    Integrates a particle ensemble from the observed spill location at
    detection_time backward in time by horizon_hours. Uses the same forcing
    interpolation and windage parameterization as the forward model
    (simulate_ensemble in particles.py), but with the displacement sign
    inverted (subtract instead of add) and no diffusion term.

    Parameters
    ----------
    observed_lat : float
        WGS-84 latitude of the observed spill centroid.
    observed_lon : float
        WGS-84 longitude of the observed spill centroid.
    detection_time : datetime
        Timezone-aware UTC datetime when the spill was detected.
        Raises ValueError if naive.
    forcing_series : Sequence[Dict[str, Any]]
        Historical forcing from forcing_loader.load_historical_forcing().
        Each dict: {t, wind_speed_ms, wind_dir_deg, current_speed_ms, current_dir_deg}.
        Must have at least 2 samples.
    horizon_hours : float
        How far back to integrate in hours. Default 24 h.
    n_particles : int
        Ensemble size. Default 500.
    seed : int
        RNG seed for reproducibility.
    windage_range : Tuple[float, float]
        (min_fraction, max_fraction) windage coefficient range.
        Default (0.01, 0.04) matches forward model.
    observed_area_m2 : float
        Area of observed spill footprint, used to initialize particle spread.
        Default 1 km².

    Returns
    -------
    BackwardEnsembleResult
        Contains trajectory_history, final_particles, final_centroid,
        and uncertainty_metrics.

    Raises
    ------
    ValueError
        For invalid inputs (see _validate_backward_inputs).
    """

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    detection_utc = _validate_backward_inputs(
        observed_lat, observed_lon, detection_time,
        forcing_series, horizon_hours, n_particles,
        seed, windage_range, observed_area_m2,
    )
    series = sorted(forcing_series, key=lambda s: s["t"])

    # ------------------------------------------------------------------
    # Particle initialization (same formula as simulate_ensemble)
    # ------------------------------------------------------------------
    rng = np.random.default_rng(seed)
    base_radius = max(MIN_RADIUS_M, math.sqrt(observed_area_m2 / math.pi))
    m_lat = 111_319.0
    m_lon = m_lat * math.cos(math.radians(observed_lat)) + 1e-10

    angles = rng.uniform(0.0, 2.0 * math.pi, n_particles)
    radii = base_radius * np.sqrt(rng.uniform(0.0, 1.0, n_particles))
    lat_p = observed_lat + radii * np.sin(angles) / m_lat
    lon_p = observed_lon + radii * np.cos(angles) / m_lon

    # ------------------------------------------------------------------
    # Windage initialization
    # ------------------------------------------------------------------
    windage_coeff = rng.uniform(windage_range[0], windage_range[1], n_particles)
    windage_resample_timer = np.zeros(n_particles)

    # ------------------------------------------------------------------
    # Integration parameters
    # ------------------------------------------------------------------
    dt = INTEGRATION_STEP_S
    total_steps = int(math.ceil(horizon_hours * 3600.0 / dt))
    trajectory_history: List[Dict[str, Any]] = []
    t_current = detection_utc

    # ------------------------------------------------------------------
    # Backward integration loop
    # ------------------------------------------------------------------
    for step in range(total_steps):
        # 1. Evaluate forcing at t_current BEFORE decrementing
        we, wn, ce, cn = _interp_forcing(series, t_current)

        # 2. Resample windage periodically
        windage_resample_timer += dt
        resample_mask = windage_resample_timer >= WINDAGE_RESAMPLE_INTERVAL_S
        if resample_mask.any():
            windage_coeff[resample_mask] = rng.uniform(
                windage_range[0], windage_range[1], int(resample_mask.sum())
            )
            windage_resample_timer[resample_mask] = 0.0

        # 3. Total velocity (per-particle windage, same as forward model)
        ve = ce + windage_coeff * we    # eastward m/s per particle
        vn = cn + windage_coeff * wn    # northward m/s per particle

        # 4. BACKWARD displacement — SUBTRACT (not add)
        # No diffusion term — diffusion is irreversible and is NOT reversed here
        lat_p -= vn * dt / m_lat
        lon_p -= ve * dt / m_lon

        # 5. Decrement time
        t_current = datetime.fromtimestamp(
            t_current.timestamp() - dt, tz=timezone.utc
        )

        # 6. Record trajectory state
        centroid_lat = float(lat_p.mean())
        centroid_lon = float(lon_p.mean())
        spread_m = _rms_spread(lat_p, lon_p, centroid_lat, centroid_lon)
        trajectory_history.append({
            "t":            t_current,
            "lat":          lat_p.copy(),    # COPY — not a reference
            "lon":          lon_p.copy(),    # COPY — not a reference
            "centroid_lat": centroid_lat,
            "centroid_lon": centroid_lon,
            "spread_m":     spread_m,
        })

    # ------------------------------------------------------------------
    # Output assembly
    # ------------------------------------------------------------------
    final_lat = lat_p.copy()
    final_lon = lon_p.copy()
    final_centroid_lat = float(final_lat.mean())
    final_centroid_lon = float(final_lon.mean())
    final_centroid = (final_centroid_lat, final_centroid_lon)

    uncertainty_metrics = {
        "rms_spread_m": _rms_spread(final_lat, final_lon, final_centroid_lat, final_centroid_lon),
        "min_lat":      float(final_lat.min()),
        "max_lat":      float(final_lat.max()),
        "min_lon":      float(final_lon.min()),
        "max_lon":      float(final_lon.max()),
        "total_steps":  total_steps,
        "dt_s":         dt,
    }

    return BackwardEnsembleResult(
        detection_time=detection_utc,
        horizon_hours=horizon_hours,
        n_particles=n_particles,
        trajectory_history=trajectory_history,
        final_particles={"lat": final_lat, "lon": final_lon},
        final_centroid=final_centroid,
        uncertainty_metrics=uncertainty_metrics,
    )
