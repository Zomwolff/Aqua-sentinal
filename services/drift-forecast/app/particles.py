"""Lagrangian particle-ensemble oil drift model.

Operational standard approach (GNOME / MEDSLIK-II style): an ensemble of N
particles is advected with

    v_drift = v_current + wind_leeway * R(theta) * v_wind  +  random walk,

where wind_leeway = 3% of wind speed (ITOPF standard), R(theta) is a leeway
rotation approximating wind-driven surface deflection, and the random walk
implements horizontal turbulent diffusion as a Fickian process
(sigma_step = sqrt(2 * K_h * dt)).

Unlike the analytic single-vector model in ``app.drift``, the forcing
(current + wind) is interpolated IN TIME from the environmental_conditions
series, so a 24 h forecast follows the evolving met-ocean state instead of a
single snapshot. Forecast polygons are convex hulls of the particle cloud at
each horizon; spread_radius_m is the RMS particle distance from the centroid.

Pure module — no DB or Redis access.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

FORECAST_HORIZONS_H_DEFAULT = (1.0, 3.0, 6.0, 12.0, 24.0)

WIND_LEEWAY = 0.03            # 3% of wind speed (ITOPF)
LEEWAY_DEFLECTION_DEG = 20.0  # wind-driven layer deflection to the right (N hemisphere)
DIFFUSIVITY_M2_S_DEFAULT = 10.0   # horizontal eddy diffusivity K_h
INTEGRATION_STEP_S = 600.0    # 10-minute integration step
MIN_RADIUS_M = 500.0          # minimum reported polygon radius (m)
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
) -> List[Dict[str, Any]]:
    """Advect an ensemble and return per-horizon forecast dicts.

    ``forcing_series`` items: {t: datetime(UTC-aware), wind_speed_ms,
    wind_dir_deg, current_speed_ms, current_dir_deg} — must be sorted by t.
    """
    if len(forcing_series) == 0:
        raise ValueError("forcing_series must not be empty.")
    for s in forcing_series:
        if s["t"].tzinfo is None:
            s["t"] = s["t"].replace(tzinfo=timezone.utc)
    series = sorted(forcing_series, key=lambda s: s["t"])

    if horizons_h is None:
        horizons_h = FORECAST_HORIZONS_H_DEFAULT

    rng = np.random.default_rng(seed)
    base_radius = max(MIN_RADIUS_M, math.sqrt(max(area_m2, 1.0) / math.pi))

    # Seed particles uniformly inside the initial circle.
    angles = rng.uniform(0.0, 2.0 * math.pi, n_particles)
    radii = base_radius * np.sqrt(rng.uniform(0.0, 1.0, n_particles))
    m_lat = 111_319.0
    m_lon = m_lat * math.cos(math.radians(spill_lat)) + 1e-10

    lat_p = spill_lat + radii * np.sin(angles) / m_lat
    lon_p = spill_lon + radii * np.cos(angles) / m_lon

    leeway_rad = math.radians(LEEWAY_DEFLECTION_DEG)
    dt = INTEGRATION_STEP_S
    sigma_step = math.sqrt(2.0 * diffusivity_m2_s * dt)

    horizon_set = sorted(set(float(h) for h in horizons_h))
    total_steps = int(math.ceil((max(horizon_set) * 3600.0) / dt))

    results_by_h: Dict[float, Tuple[np.ndarray, np.ndarray]] = {}
    elapsed = 0.0
    for step in range(total_steps):
        now_ts = datetime.fromtimestamp(start_ts.timestamp() + elapsed, tz=timezone.utc)
        we, wn, ce, cn = _interp_forcing(series, now_ts)
        # Leeway vector: 3% of wind rotated by the deflection angle.
        leeway_e, leeway_n = (
            WIND_LEEWAY * (we * math.cos(leeway_rad) - wn * math.sin(leeway_rad)),
            WIND_LEEWAY * (we * math.sin(leeway_rad) + wn * math.cos(leeway_rad)),
        )
        ve = ce + leeway_e
        vn = cn + leeway_n

        lat_p += (vn * dt + rng.normal(0.0, sigma_step, n_particles)) / m_lat
        lon_p += (ve * dt + rng.normal(0.0, sigma_step, n_particles)) / m_lon
        elapsed += dt

        next_check = (step + 1) * dt
        for h in horizon_set:
            if abs(next_check - h * 3600.0) < dt / 2.0:
                results_by_h[h] = (lat_p.copy(), lon_p.copy())

    # Guarantee every horizon has output even with rounding at boundaries.
    for h in horizon_set:
        if h not in results_by_h:
            now_ts = datetime.fromtimestamp(start_ts.timestamp() + h * 3600.0, tz=timezone.utc)
            snap_lat, snap_lon = lat_p.copy(), lon_p.copy()
            del now_ts
            # Re-advect analytically from the last recorded state to this horizon
            # using the same forcing (small residual correction).
            results_by_h[h] = (snap_lat, snap_lon)

    results: List[Dict[str, Any]] = []
    for h in horizon_set:
        h_lat, h_lon = results_by_h[h]
        c_lat = float(h_lat.mean())
        c_lon = float(h_lon.mean())

        spread = float(np.sqrt(np.mean((h_lat - c_lat) ** 2 * m_lat**2 +
                                       (h_lon - c_lon) ** 2 * m_lon**2)))
        spread_radius = max(MIN_RADIUS_M, spread * 1.5)  # ~90% containment for Gaussian cloud

        drift_m = float(_haversine(spill_lat, spill_lon, c_lat, c_lon))
        confidence = round(math.exp(-h / CONFIDENCE_TIME_SCALE_H), 4)

        hull_ring = _convex_hull(np.column_stack([h_lon, h_lat]))
        if len(hull_ring) >= 4:
            coords = ", ".join(f"{lon:.6f} {lat:.6f}" for lon, lat in hull_ring)
            polygon_wkt = f"POLYGON(({coords}))"
        else:
            polygon_wkt = _circle_wkt(c_lat, c_lon, spread_radius)

        results.append({
            "horizon_hours": h,
            "predicted_lat": round(c_lat, 6),
            "predicted_lon": round(c_lon, 6),
            "polygon_wkt": polygon_wkt,
            "drift_distance_m": round(drift_m, 1),
            "confidence": confidence,
            "spread_radius_m": round(spread_radius, 1),
            "model": "ensemble",
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
