"""
Severity Impact — multi-factor severity scorecard.

Computes environmental, population, economic, and protected-area risk for
a detected oil spill, using only data already present in PostGIS:

  area_score           (30%) — area_m2 normalised against 10 km2 saturation
  protected_area_risk  (30%) — PostGIS ST_DWithin against protected_areas
  population_risk      (20%) — distance to coastline (reference_layers)
  environmental_risk   (20%) — combination of area + protected area proximity

Final severity_score → [0,1]. Tier:
  score < 0.30  → LOW
  0.30–0.60     → MODERATE
  0.60–0.80     → HIGH
  score >= 0.80 → CRITICAL

Pure module — no DB/Redis.
"""
from __future__ import annotations
import os
from typing import Dict, List

SEVERITY_AREA_SATURATION_M2   = float(os.environ.get("SEVERITY_AREA_SATURATION_M2", 10_000_000))  # 10 km2
SEVERITY_PROTECTED_RADIUS_M   = float(os.environ.get("SEVERITY_PROTECTED_RADIUS_M", 10_000))     # 10 km
SEVERITY_COAST_RADIUS_M       = float(os.environ.get("SEVERITY_COAST_RADIUS_M", 20_000))         # 20 km

W_AREA       = 0.30
W_PROTECTED  = 0.30
W_POPULATION = 0.20
W_ENV        = 0.20

PROTECTED_TYPE_WEIGHTS = {
    "marine_protected_area": 1.0,
    "mangrove":              1.0,
    "fishing_zone":          0.7,
    "port":                  0.5,
    "eez":                   0.3,
}


def score_area(area_m2: float) -> float:
    """Larger spill → higher score, saturating at SEVERITY_AREA_SATURATION_M2."""
    return float(min(1.0, area_m2 / SEVERITY_AREA_SATURATION_M2))


def score_protected_areas(nearby_areas: List[Dict]) -> float:
    """
    Score based on which types of protected areas are nearby.
    Each area contributes its type weight, decaying with distance.
    Result is clamped to [0, 1].
    """
    if not nearby_areas:
        return 0.0
    total = 0.0
    for pa in nearby_areas:
        area_type = pa.get("area_type", "")
        distance_m = float(pa.get("distance_m", SEVERITY_PROTECTED_RADIUS_M))
        type_weight = PROTECTED_TYPE_WEIGHTS.get(area_type, 0.3)
        proximity_factor = max(0.0, 1.0 - distance_m / SEVERITY_PROTECTED_RADIUS_M)
        total += type_weight * proximity_factor
    return float(min(1.0, total))


def score_population_risk(coast_distance_m: float) -> float:
    """
    Population risk from coastal proximity.
    1.0 if spill is AT the coast, decaying to 0.0 at SEVERITY_COAST_RADIUS_M.
    """
    if coast_distance_m <= 0:
        return 1.0
    if coast_distance_m >= SEVERITY_COAST_RADIUS_M:
        return 0.0
    return float(1.0 - coast_distance_m / SEVERITY_COAST_RADIUS_M)


def score_environmental_risk(area_score: float, protected_score: float) -> float:
    """Environmental risk = combination of area scale and protected area proximity."""
    return float(min(1.0, 0.5 * area_score + 0.5 * protected_score))


def compute_severity(
    area_m2: float,
    nearby_protected_areas: List[Dict],
    coast_distance_m: float,
) -> Dict:
    """
    Compute full severity score and tier.
    Returns dict with all factor scores, final score, and tier.
    """
    a_score   = score_area(area_m2)
    p_score   = score_protected_areas(nearby_protected_areas)
    pop_score = score_population_risk(coast_distance_m)
    env_score = score_environmental_risk(a_score, p_score)

    final = (W_AREA * a_score + W_PROTECTED * p_score +
             W_POPULATION * pop_score + W_ENV * env_score)
    final = round(float(min(1.0, max(0.0, final))), 4)

    if final >= 0.80:
        tier = "CRITICAL"
    elif final >= 0.60:
        tier = "HIGH"
    elif final >= 0.30:
        tier = "MODERATE"
    else:
        tier = "LOW"

    return {
        "score": final,
        "severity_level": tier,
        "area_score": round(a_score, 4),
        "protected_area_risk": round(p_score, 4),
        "population_risk": round(pop_score, 4),
        "environmental_risk": round(env_score, 4),
        "protected_areas_nearby": len(nearby_protected_areas),
        "coast_distance_m": round(coast_distance_m, 1),
    }
