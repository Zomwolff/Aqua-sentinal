"""
Drift Forecast — Lagrangian particle-advection oil drift model.

Uses the standard operational formula used in GNOME / MEDSLIK-II:
    drift_vector = current_velocity + 0.03 * wind_velocity

Generates predicted spill polygons at 5 time horizons: 1h, 3h, 6h, 12h, 24h.
For each horizon:
  - The spill centroid is advected along the drift vector.
  - The spill polygon is translated + grown using Fickian diffusion:
      spread_radius = base_radius * sqrt(1 + t_hours / t_scale)
    This models turbulent diffusion increasing with time.

All geometry is computed in Python using spherical approximations.
Final polygons are WKT strings for insertion into PostGIS.

Pure module — no DB or Redis access.
"""
from __future__ import annotations

import math
import os
from typing import Dict, List, Optional, Tuple

FORECAST_HORIZONS_H = [1.0, 3.0, 6.0, 12.0, 24.0]
DIFFUSION_TIME_SCALE_H = 12.0  # hours at which spread doubles (sqrt(2))
WIND_LEEWAY = 0.03             # 3% wind leeway (ITOPF standard)
MIN_RADIUS_M = 500.0           # minimum forecast polygon radius (m)


def _haversine(lat1, lon1, lat2, lon2):
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.asin(math.sqrt(max(0.0, a)))


def _wind_components(speed_ms: float, direction_deg: float) -> Tuple[float, float]:
    """Decompose speed+direction into (east_ms, north_ms). Meteorological convention."""
    rad = math.radians(direction_deg)
    return -speed_ms * math.sin(rad), -speed_ms * math.cos(rad)


def _advect_position(
    lat: float, lon: float,
    wind_speed_ms: float, wind_dir_deg: float,
    current_speed_ms: float, current_dir_deg: float,
    elapsed_hours: float,
) -> Tuple[float, float]:
    """
    Advect a point using the 3%-wind + 100%-current formula.
    Returns (new_lat, new_lon).
    """
    elapsed_s = elapsed_hours * 3600.0
    we, wn = _wind_components(wind_speed_ms, wind_dir_deg)
    ce, cn = _wind_components(current_speed_ms, current_dir_deg)
    total_e = (ce + WIND_LEEWAY * we) * elapsed_s
    total_n = (cn + WIND_LEEWAY * wn) * elapsed_s
    m_lat = 111_319.0
    m_lon = 111_319.0 * math.cos(math.radians(lat)) + 1e-10
    return lat + total_n / m_lat, lon + total_e / m_lon


def _circle_polygon_wkt(
    center_lat: float, center_lon: float,
    radius_m: float,
    n_points: int = 32,
) -> str:
    """
    Approximate a circle as a WKT POLYGON with n_points vertices.
    Uses spherical approximation (accurate to <0.01% for radii under 100 km).
    """
    m_lat = 111_319.0
    m_lon = 111_319.0 * math.cos(math.radians(center_lat)) + 1e-10
    coords = []
    for i in range(n_points):
        angle = 2 * math.pi * i / n_points
        dlat = radius_m * math.cos(angle) / m_lat
        dlon = radius_m * math.sin(angle) / m_lon
        coords.append(f"{center_lon + dlon:.6f} {center_lat + dlat:.6f}")
    coords.append(coords[0])  # close the ring
    return f"POLYGON(({', '.join(coords)}))"


def _estimate_spill_radius(area_m2: float) -> float:
    """Approximate equivalent-circle radius from the spill polygon area."""
    return max(MIN_RADIUS_M, math.sqrt(area_m2 / math.pi))


def forecast_spill(
    spill_lat: float,
    spill_lon: float,
    area_m2: float,
    wind_speed_ms: float,
    wind_dir_deg: float,
    current_speed_ms: float,
    current_dir_deg: float,
    horizons_h: Optional[List[float]] = None,
) -> List[Dict]:
    """
    Generate drift forecasts for the given spill at each time horizon.

    Returns a list of dicts, one per horizon:
        horizon_hours, predicted_lat, predicted_lon, polygon_wkt,
        drift_distance_m, confidence, spread_radius_m

    Confidence decays with horizon: 0.9 at 1h, ~0.5 at 24h.
    Fickian diffusion: spread_radius = base * sqrt(1 + t / t_scale)
    """
    if horizons_h is None:
        horizons_h = FORECAST_HORIZONS_H

    base_radius = _estimate_spill_radius(area_m2)
    results = []

    for t_h in horizons_h:
        # Advect centroid
        pred_lat, pred_lon = _advect_position(
            spill_lat, spill_lon,
            wind_speed_ms, wind_dir_deg,
            current_speed_ms, current_dir_deg,
            t_h,
        )
        drift_m = _haversine(spill_lat, spill_lon, pred_lat, pred_lon)

        # Spread radius grows with sqrt(time)
        spread_factor = math.sqrt(1.0 + t_h / DIFFUSION_TIME_SCALE_H)
        spread_radius = max(MIN_RADIUS_M, base_radius * spread_factor)

        # Confidence decays: C(t) = exp(-t / 36) → 0.97 at 1h, 0.92 at 3h, 0.85 at 6h, 0.72 at 12h, 0.51 at 24h
        confidence = round(math.exp(-t_h / 36.0), 4)

        polygon_wkt = _circle_polygon_wkt(pred_lat, pred_lon, spread_radius)

        results.append({
            "horizon_hours": t_h,
            "predicted_lat": round(pred_lat, 6),
            "predicted_lon": round(pred_lon, 6),
            "polygon_wkt": polygon_wkt,
            "drift_distance_m": round(drift_m, 1),
            "spread_radius_m": round(spread_radius, 1),
            "confidence": confidence,
        })

    return results
