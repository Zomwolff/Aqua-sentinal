"""Lagrangian particle-ensemble oil drift model.

SCIENTIFIC ARCHITECTURE (V2 - Separated Physics):

This module now explicitly separates three distinct physical processes:

1. DRIFT / ADVECTION
   - Transport by ocean currents (100%)
   - Wind-driven surface drift (windage, typically 1-4% of wind speed)
   - Optional wind deflection angle (engineering parameter, configurable)

2. PHYSICAL OIL SPREADING
   - Gravity-viscous spreading (Fay model)
   - Independent of transport/advection
   - Computed in oil_physics.py module

3. UNCERTAINTY / DIFFUSION
   - Fickian random walk (sub-grid turbulent diffusion)
   - Forecast uncertainty from ensemble spread
   - Probability contours computed in uncertainty.py module

IMPORTANT CHANGES FROM V1:
- Windage is NO LONGER a fixed 3% constant; it is sampled per particle (1-4% range)
- Wind deflection is now optional/configurable, not a universal 20° constant
- Particle RMS spread represents UNCERTAINTY, not physical oil radius
- Convex hull is for visualization, NOT a probability contour
- Physical spreading is computed separately using Fay formulation

Pure module — no DB or Redis access.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# Import separated physics modules
from app.oil_physics import compute_physical_area, compute_physical_spread_radius, OilProperties
from app.uncertainty import compute_uncertainty_footprint

FORECAST_HORIZONS_H_DEFAULT = (1.0, 3.0, 6.0, 12.0, 24.0)

# ============================================================================
# WINDAGE PARAMETERIZATION (V2: No longer a universal constant)
# ============================================================================
# SCIENTIFIC NOTE:
# Windage (wind-drift coefficient) varies with oil type, slick thickness,
# wind speed, and sea state. Operational models use 1-4% range.
# ITOPF guidance suggests ~3% as a central estimate, NOT a universal law.

WINDAGE_MIN = float(os.environ.get("WINDAGE_MIN_PERCENT", 1.0)) / 100.0  # 1%
WINDAGE_MAX = float(os.environ.get("WINDAGE_MAX_PERCENT", 4.0)) / 100.0  # 4%
WINDAGE_RESAMPLE_INTERVAL_S = float(os.environ.get("WINDAGE_RESAMPLE_INTERVAL_S", 900.0))  # 15 min

# ============================================================================
# WIND DEFLECTION (V2: Optional engineering parameter, not universal constant)
# ============================================================================
# SCIENTIFIC NOTE:
# Wind-driven surface layer may deflect due to Ekman/Coriolis effects.
# Magnitude is NOT universally 20° - it varies with latitude, wind duration,
# stratification, etc. For V1 compatibility, we keep the option configurable.

LEEWAY_DEFLECTION_ENABLED = os.environ.get("WIND_DEFLECTION_ENABLED", "false").lower() == "true"
LEEWAY_DEFLECTION_DEG = float(os.environ.get("WIND_DEFLECTION_DEG", 20.0))  # Only used if enabled

# ============================================================================
# DIFFUSION (V2: Unchanged, but clarified as engineering approximation)
# ============================================================================
# SCIENTIFIC NOTE:
# Constant K_h = 10 m²/s is a pragmatic approximation.
# Real ocean diffusivity is scale-dependent (Okubo diagram).
# For operational forecast at 1-50 km scales, K_h ~ 1-100 m²/s is reasonable.

DIFFUSIVITY_M2_S_DEFAULT = float(os.environ.get("DRIFT_DIFFUSIVITY_M2_S", 10.0))

# Integration timestep
INTEGRATION_STEP_S = 600.0    # 10-minute integration step

# Minimum radius for safety (prevents degenerate geometries)
MIN_RADIUS_M = 500.0

# Confidence decay (unchanged, operational heuristic)
CONFIDENCE_TIME_SCALE_H = 36.0


def _wind_components(speed_ms: float, direction_deg: float) -> Tuple[float, float]:
    """Meteorological convention: direction is where the wind blows FROM."""
    rad = math.radians(direction_deg)
    return -speed_ms * math.sin(rad), -speed_ms * math.cos(rad)


def _rotate(east: float, north: float, angle_deg: float) -> Tuple[float, float]:
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    # Right-handed rotation on screen coords: (e,n) -> (e*cos - n*sin, e*sin + n*cos)
    return east * cos_a - north * sin_a, east * sin_a + north * cos_a


def _sample_windage(rng: np.random.Generator, n_particles: int) -> np.ndarray:
    """
    Sample windage coefficient for each particle.
    
    V2: Windage is no longer a universal 3% constant.
    Sample from configured range (default 1-4%) to represent uncertainty.
    
    Parameters:
        rng: Random number generator
        n_particles: Number of particles
    
    Returns:
        Array of windage coefficients (dimensionless fraction)
    """
    return rng.uniform(WINDAGE_MIN, WINDAGE_MAX, n_particles)


def _interp_forcing(
    series: Sequence[Dict[str, Any]],
    ts: datetime,
) -> Tuple[float, float, float, float]:
    """Linear interpolation in time of (wind_e, wind_n, cur_e, cur_n).

    Holds nearest sample outside the covered range (persistence forecast).
    """
    target = ts.timestamp()

    def _comp(sample: Dict[str, Any]) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        we, wn = _wind_components(float(sample["wind_speed_ms"]), float(sample["wind_dir_deg"]))
        ce, cn = _wind_components(float(sample["current_speed_ms"]), float(sample["current_dir_deg"]))
        return (we, wn), (ce, cn)

    first = series[0]
    if target <= first["t"].timestamp():
        (we, wn), (ce, cn) = _comp(first)
        return we, wn, ce, cn

    last = series[-1]
    if target >= last["t"].timestamp():
        (we, wn), (ce, cn) = _comp(last)
        return we, wn, ce, cn

    lo = first
    hi = last
    for i in range(1, len(series)):
        if series[i]["t"].timestamp() >= target:
            lo, hi = series[i - 1], series[i]
            break
    span = hi["t"].timestamp() - lo["t"].timestamp()
    frac = 0.0 if span <= 0 else (target - lo["t"].timestamp()) / span
    (wlo_e, wlo_n), (clo_e, clo_n) = _comp(lo)
    (whi_e, whi_n), (chi_e, chi_n) = _comp(hi)
    return (
        wlo_e + (whi_e - wlo_e) * frac,
        wlo_n + (whi_n - wlo_n) * frac,
        clo_e + (chi_e - clo_e) * frac,
        clo_n + (chi_n - clo_n) * frac,
    )


def _convex_hull(points: np.ndarray) -> List[Tuple[float, float]]:
    """Monotone-chain convex hull over (lon, lat) points. Returns closed ring."""
    pts = sorted({(round(float(p[0]), 7), round(float(p[1]), 7)) for p in points})
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    ring = lower[:-1] + upper[:-1]
    ring.append(ring[0])
    return ring


def simulate_ensemble(
    spill_lat: float,
    spill_lon: float,
    area_m2: float,
    start_ts: datetime,
    forcing_series: Sequence[Dict[str, Any]],
    horizons_h: Optional[Sequence[float]] = None,
    n_particles: int = 500,
    diffusivity_m2_s: float = DIFFUSIVITY_M2_S_DEFAULT,
    seed: int = 42,
    oil_properties: Optional[OilProperties] = None,
) -> List[Dict[str, Any]]:
    """Advect an ensemble and return per-horizon forecast dicts.
    
    V2 CHANGES:
    - Windage now sampled per particle from configurable range
    - Wind deflection optional (disabled by default in V2)
    - Physical spreading computed separately via Fay model
    - Uncertainty represented by probability contours, not RMS × 1.5
    - Outputs clearly separate drift, physical spread, and uncertainty

    ``forcing_series`` items: {t: datetime(UTC-aware), wind_speed_ms,
    wind_dir_deg, current_speed_ms, current_dir_deg} — must be sorted by t.
    
    Parameters:
        oil_properties: Optional OilProperties for physical spreading model
    """
    if len(forcing_series) == 0:
        raise ValueError("forcing_series must not be empty.")
    for s in forcing_series:
        if s["t"].tzinfo is None:
            s["t"] = s["t"].replace(tzinfo=timezone.utc)
    series = sorted(forcing_series, key=lambda s: s["t"])

    if horizons_h is None:
        horizons_h = FORECAST_HORIZONS_H_DEFAULT
    
    if oil_properties is None:
        oil_properties = OilProperties()  # Use defaults

    rng = np.random.default_rng(seed)
    base_radius = max(MIN_RADIUS_M, math.sqrt(max(area_m2, 1.0) / math.pi))

    # Seed particles uniformly inside the initial circle.
    angles = rng.uniform(0.0, 2.0 * math.pi, n_particles)
    radii = base_radius * np.sqrt(rng.uniform(0.0, 1.0, n_particles))
    m_lat = 111_319.0
    m_lon = m_lat * math.cos(math.radians(spill_lat)) + 1e-10

    lat_p = spill_lat + radii * np.sin(angles) / m_lat
    lon_p = spill_lon + radii * np.cos(angles) / m_lon

    # V2: Sample windage per particle
    windage_coeff = _sample_windage(rng, n_particles)
    windage_resample_timer = np.zeros(n_particles)  # Time since last resample

    # Wind deflection (optional in V2)
    if LEEWAY_DEFLECTION_ENABLED:
        leeway_rad = math.radians(LEEWAY_DEFLECTION_DEG)
        cos_deflect = math.cos(leeway_rad)
        sin_deflect = math.sin(leeway_rad)
    else:
        cos_deflect = 1.0
        sin_deflect = 0.0

    dt = INTEGRATION_STEP_S
    sigma_step = math.sqrt(2.0 * diffusivity_m2_s * dt)

    horizon_set = sorted(set(float(h) for h in horizons_h))
    total_steps = int(math.ceil((max(horizon_set) * 3600.0) / dt))

    results_by_h: Dict[float, Tuple[np.ndarray, np.ndarray]] = {}
    elapsed = 0.0
    
    for step in range(total_steps):
        now_ts = datetime.fromtimestamp(start_ts.timestamp() + elapsed, tz=timezone.utc)
        we, wn, ce, cn = _interp_forcing(series, now_ts)
        
        # V2: Resample windage periodically
        windage_resample_timer += dt
        resample_mask = windage_resample_timer >= WINDAGE_RESAMPLE_INTERVAL_S
        if resample_mask.any():
            windage_coeff[resample_mask] = _sample_windage(rng, resample_mask.sum())
            windage_resample_timer[resample_mask] = 0.0
        
        # Leeway vector: windage% of wind, optionally deflected
        leeway_e = windage_coeff * (we * cos_deflect - wn * sin_deflect)
        leeway_n = windage_coeff * (we * sin_deflect + wn * cos_deflect)
        
        # Total velocity: current + leeway
        ve = ce + leeway_e
        vn = cn + leeway_n

        # Advection + diffusion
        lat_p += (vn * dt + rng.normal(0.0, sigma_step, n_particles)) / m_lat
        lon_p += (ve * dt + rng.normal(0.0, sigma_step, n_particles)) / m_lon
        elapsed += dt

        next_check = (step + 1) * dt
        for h in horizon_set:
            if abs(next_check - h * 3600.0) < dt / 2.0:
                results_by_h[h] = (lat_p.copy(), lon_p.copy())

    # Guarantee every horizon has output
    for h in horizon_set:
        if h not in results_by_h:
            results_by_h[h] = (lat_p.copy(), lon_p.copy())

    # ========================================================================
    # V2: PROCESS OUTPUTS WITH SEPARATED PHYSICS
    # ========================================================================
    results: List[Dict[str, Any]] = []
    
    for h in horizon_set:
        h_lat, h_lon = results_by_h[h]
        
        # ====================================================================
        # 1. DRIFT / ADVECTION (centroid displacement)
        # ====================================================================
        centroid_lat = float(h_lat.mean())
        centroid_lon = float(h_lon.mean())
        drift_m = float(_haversine(spill_lat, spill_lon, centroid_lat, centroid_lon))
        
        # Drift velocity
        drift_velocity_ms = drift_m / (h * 3600.0) if h > 0 else 0.0
        
        # Drift bearing (degrees from north)
        dlat = centroid_lat - spill_lat
        dlon = centroid_lon - spill_lon
        if abs(dlat) < 1e-9 and abs(dlon) < 1e-9:
            bearing_deg = 0.0
        else:
            bearing_rad = math.atan2(dlon, dlat)
            bearing_deg = math.degrees(bearing_rad) % 360.0
        
        # ====================================================================
        # 2. PHYSICAL OIL SPREADING (Fay model)
        # ====================================================================
        physical_area_m2 = compute_physical_area(
            initial_area_m2=area_m2,
            elapsed_hours=h,
            oil_density=oil_properties.density,
            oil_viscosity=oil_properties.viscosity,
            water_density=1025.0,
            initial_thickness_m=oil_properties.thickness,
        )
        physical_radius_m = compute_physical_spread_radius(
            initial_area_m2=area_m2,
            elapsed_hours=h,
            oil_density=oil_properties.density,
            oil_viscosity=oil_properties.viscosity,
            water_density=1025.0,
            initial_thickness_m=oil_properties.thickness,
        )
        
        expansion_ratio = physical_area_m2 / area_m2 if area_m2 > 0 else 1.0
        spread_rate_m2h = (physical_area_m2 - area_m2) / h if h > 0 else 0.0
        
        # ====================================================================
        # 3. UNCERTAINTY / PROBABILITY FOOTPRINT
        # ====================================================================
        uncertainty_result = compute_uncertainty_footprint(
            lat_particles=h_lat,
            lon_particles=h_lon,
            method="grid",  # Use grid-based for V1 (faster, sufficient)
            probability_levels=[0.50, 0.90],
        )
        
        rms_spread_m = uncertainty_result["rms_spread_m"]
        prob_50_wkt = uncertainty_result["probability_contours"].get("0.50")
        prob_90_wkt = uncertainty_result["probability_contours"].get("0.90")
        convex_hull_wkt = uncertainty_result["convex_hull_wkt"]
        
        # ====================================================================
        # 4. BEST-ESTIMATE GEOMETRY (Physical spread + drift)
        # ====================================================================
        # Place physical spreading circle at drift-advected centroid
        best_estimate_wkt = _circle_wkt(centroid_lat, centroid_lon, physical_radius_m)
        
        # ====================================================================
        # 5. CONFIDENCE (time-decay heuristic, unchanged)
        # ====================================================================
        confidence = round(math.exp(-h / CONFIDENCE_TIME_SCALE_H), 4)

        # ====================================================================
        # ASSEMBLE OUTPUT
        # ====================================================================
        results.append({
            "horizon_hours": h,
            "predicted_lat": round(centroid_lat, 6),
            "predicted_lon": round(centroid_lon, 6),
            
            # Geometry outputs
            "polygon_wkt": best_estimate_wkt,  # CHANGED: Physical spread at drift location
            "probability_50_wkt": prob_50_wkt,
            "probability_90_wkt": prob_90_wkt,
            "convex_hull_wkt": convex_hull_wkt,  # For reference/visualization only
            
            # Drift metrics
            "drift_distance_m": round(drift_m, 1),
            "drift_velocity_ms": round(drift_velocity_ms, 3),
            "drift_bearing_deg": round(bearing_deg, 1),
            
            # Physical spreading metrics
            "physical_area_m2": round(physical_area_m2, 2),
            "physical_radius_m": round(physical_radius_m, 1),
            "expansion_ratio": round(expansion_ratio, 4),
            "spread_rate_m2_per_hour": round(spread_rate_m2h, 2),
            
            # Uncertainty metrics
            "uncertainty_rms_m": round(rms_spread_m, 1),
            "uncertainty_std_east_m": round(uncertainty_result["std_east_m"], 1),
            "uncertainty_std_north_m": round(uncertainty_result["std_north_m"], 1),
            
            # Metadata
            "confidence": confidence,
            "model": "ensemble-v2-separated-physics",
            "oil_properties": oil_properties.to_dict(),
            "windage_range": [WINDAGE_MIN, WINDAGE_MAX],
            "wind_deflection_enabled": LEEWAY_DEFLECTION_ENABLED,
        })

    return results


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(max(0.0, a)))


def _circle_wkt(center_lat: float, center_lon: float, radius_m: float, n_points: int = 32) -> str:
    m_lat = 111_319.0
    m_lon = 111_319.0 * math.cos(math.radians(center_lat)) + 1e-10
    coords = []
    for i in range(n_points):
        angle = 2 * math.pi * i / n_points
        dlat = radius_m * math.cos(angle) / m_lat
        dlon = radius_m * math.sin(angle) / m_lon
        coords.append(f"{center_lon + dlon:.6f} {center_lat + dlat:.6f}")
    coords.append(coords[0])
    return f"POLYGON(({', '.join(coords)}))"
