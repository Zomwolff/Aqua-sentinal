"""
Source Attribution: 5-factor probabilistic scoring engine.

Factors (weights):
  distance_score   (0.25) — geography proximity of vessel to spill centroid
  trajectory_score (0.25) — does vessel AIS track pass through spill zone?
  time_score       (0.20) — vessel present near SAR acquisition time?
  behavior_score   (0.15) — HIGH anomaly events in 6h window before spill?
  wind_score       (0.15) — backward drift from spill traces back to vessel?

Pure module (no DB/Redis). Worker handles all async IO.

IMPORTANT: High score = strong circumstantial evidence only.
Labels: probable_source, possible_source, correlated, insufficient_evidence.
Never: confirmed, guilty, responsible.
"""
from __future__ import annotations
import math, os
from typing import Optional, Tuple

ATTRIBUTION_SPATIAL_WINDOW_M  = float(os.environ.get("ATTRIBUTION_SPATIAL_WINDOW_M", 20_000))
ATTRIBUTION_TEMPORAL_WINDOW_H = float(os.environ.get("ATTRIBUTION_TEMPORAL_WINDOW_H", 6.0))
MODEL_VERSION = "1.1"

W_DISTANCE   = 0.25
W_TRAJECTORY = 0.25
W_TIME       = 0.20
W_BEHAVIOR   = 0.15
W_WIND       = 0.15


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlam = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.asin(math.sqrt(max(0.0, a)))


def score_distance(distance_m: float, window_m: float = ATTRIBUTION_SPATIAL_WINDOW_M) -> float:
    """1.0 at distance=0, linear decay to 0.0 at window_m."""
    if distance_m <= 0: return 1.0
    if distance_m >= window_m: return 0.0
    return float(1.0 - distance_m / window_m)


def score_trajectory(closest_approach_m: Optional[float],
                     window_m: float = ATTRIBUTION_SPATIAL_WINDOW_M) -> float:
    """Based on closest approach of AIS track to spill centroid during time window."""
    if closest_approach_m is None: return 0.0
    return score_distance(closest_approach_m, window_m)


def score_time(hours_from_acquisition: Optional[float],
               window_h: float = ATTRIBUTION_TEMPORAL_WINDOW_H) -> float:
    """1.0 at acquisition time, linear decay to 0 at ±window_h."""
    if hours_from_acquisition is None: return 0.0
    gap = abs(hours_from_acquisition)
    if gap >= window_h: return 0.0
    return float(1.0 - gap / window_h)


def score_behavior(high_anomaly_count: int, medium_anomaly_count: int) -> float:
    """HIGH anomalies weight 3x, MEDIUM 1x. Saturates at 10 weighted events."""
    return float(min(1.0, (high_anomaly_count * 3 + medium_anomaly_count) / 10.0))


def score_wind_drift(
    vessel_lat: float, vessel_lon: float,
    spill_lat: float, spill_lon: float,
    wind_speed_ms: float, wind_dir_deg: float,
    current_speed_ms: float, current_dir_deg: float,
    elapsed_hours: float,
) -> float:
    """
    Backward drift using standard 3%-wind + 100%-current formula (GNOME/MEDSLIK-II).
    Projects spill centroid backward by elapsed_hours, measures distance to vessel.
    Score 1.0 within 5km, decays to 0 at 50km.
    """
    WIND_LEEWAY, INNER_M, OUTER_M = 0.03, 5_000.0, 50_000.0
    if elapsed_hours <= 0 or (wind_speed_ms == 0 and current_speed_ms == 0): return 0.0
    elapsed_s = elapsed_hours * 3600.0

    def comp(speed, deg):
        rad = math.radians(deg)
        return -speed*math.sin(rad), -speed*math.cos(rad)

    we, wn = comp(wind_speed_ms, wind_dir_deg)
    ce, cn = comp(current_speed_ms, current_dir_deg)
    te = (ce + WIND_LEEWAY*we) * elapsed_s
    tn = (cn + WIND_LEEWAY*wn) * elapsed_s
    m_lat = 111_319.0
    m_lon = 111_319.0 * math.cos(math.radians(spill_lat)) + 1e-10
    origin_lat = spill_lat - tn / m_lat
    origin_lon = spill_lon - te / m_lon
    dist_m = _haversine(origin_lat, origin_lon, vessel_lat, vessel_lon)
    if dist_m <= INNER_M: return 1.0
    if dist_m >= OUTER_M: return 0.0
    return float(1.0 - (dist_m - INNER_M) / (OUTER_M - INNER_M))


def compute_attribution_score(
    distance_score: float, trajectory_score: float,
    time_score: float, behavior_score: float, wind_score: float,
) -> float:
    raw = (W_DISTANCE*distance_score + W_TRAJECTORY*trajectory_score +
           W_TIME*time_score + W_BEHAVIOR*behavior_score + W_WIND*wind_score)
    return round(float(max(0.0, min(1.0, raw))), 4)


def attribution_label(final_score: float) -> str:
    if final_score >= 0.75: return "probable_source"
    elif final_score >= 0.50: return "possible_source"
    elif final_score >= 0.25: return "correlated"
    return "insufficient_evidence"
