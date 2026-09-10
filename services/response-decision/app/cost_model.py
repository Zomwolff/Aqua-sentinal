"""Planning cost estimates for oil-spill response.

The volume estimate is deliberately labelled as a planning approximation: SAR
provides slick area, not thickness. The default thickness is 0.1 mm.
"""
from __future__ import annotations

from typing import Any, Dict, List

OIL_DENSITY_KG_PER_M3 = 850.0
DEFAULT_SLICK_THICKNESS_M = 0.0001
KG_PER_TONNE = 1000.0

# Indicative planning rates, in USD per tonne recovered/managed.
_TIER_RATES = {
    "TIER_1": {"mobilisation": 25_000.0, "hourly": 8_000.0, "tonne": 1_200.0},
    "TIER_2": {"mobilisation": 75_000.0, "hourly": 18_000.0, "tonne": 1_800.0},
    "TIER_3": {"mobilisation": 250_000.0, "hourly": 45_000.0, "tonne": 2_500.0},
}


def estimate_volume_tonnes(
    area_km2: float,
    thickness_m: float = DEFAULT_SLICK_THICKNESS_M,
    density_kg_m3: float = OIL_DENSITY_KG_PER_M3,
) -> float:
    """Estimate slick mass from area and assumed thickness.

    This is a planning approximation, not a measured volume.
    """
    area_m2 = max(0.0, float(area_km2)) * 1_000_000.0
    return area_m2 * max(0.0, float(thickness_m)) * max(0.0, float(density_kg_m3)) / KG_PER_TONNE


def nosdcp_tier(severity_level: str, area_km2: float, population_risk: float = 0.0) -> str:
    """Map the current severity signals to a practical NOSDCP response tier."""
    severity = str(severity_level or "LOW").upper()
    area = max(0.0, float(area_km2))
    risk = max(0.0, float(population_risk))
    if severity == "CRITICAL" or area >= 10.0 or risk >= 0.8:
        return "TIER_3"
    if severity == "HIGH" or area >= 1.0 or risk >= 0.5:
        return "TIER_2"
    return "TIER_1"


def project_cost(
    area_km2: float,
    severity_level: str,
    population_risk: float = 0.0,
    horizons: tuple[int, ...] = (1, 3, 6, 12, 24),
) -> Dict[str, Any]:
    """Return point/low/high USD and INR estimates plus a time-stepped curve."""
    tier = nosdcp_tier(severity_level, area_km2, population_risk)
    volume = estimate_volume_tonnes(area_km2)
    rate = _TIER_RATES[tier]
    curve: List[Dict[str, Any]] = []
    for horizon in horizons:
        point = rate["mobilisation"] + rate["hourly"] * horizon + rate["tonne"] * volume
        curve.append({
            "horizon_hours": horizon,
            "point_usd": round(point, 2),
            "low_usd": round(point * 0.75, 2),
            "high_usd": round(point * 1.35, 2),
        })
    final = curve[-1]
    usd_to_inr = 95.0
    return {
        "nosdcp_tier": tier,
        "estimated_volume_tonnes": round(volume, 3),
        "volume_basis": "Planning approximation from area and assumed 0.1 mm slick thickness; SAR does not measure thickness.",
        "point_usd": final["point_usd"],
        "low_usd": final["low_usd"],
        "high_usd": final["high_usd"],
        "point_inr": round(final["point_usd"] * usd_to_inr, 2),
        "low_inr": round(final["low_usd"] * usd_to_inr, 2),
        "high_inr": round(final["high_usd"] * usd_to_inr, 2),
        "cost_curve": curve,
    }
