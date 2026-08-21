"""
Response Decision rules engine.
Generates specific, actionable response recommendations based on:
- Severity tier (CRITICAL / HIGH / MODERATE / LOW)
- Protected area proximity
- Vessel type of top attributed vessel
- Forecast trajectory (nearest 3h forecast polygon)
Pure module - no DB/Redis.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

TIER_PRIORITY = {"CRITICAL": "URGENT", "HIGH": "HIGH", "MODERATE": "MEDIUM", "LOW": "LOW"}

COAST_GUARD_AUTHORITY = "Indian Coast Guard District HQ Mumbai"
MARINE_POLLUTION_TEAM = "Marine Pollution Response Team (MPRT) - Mumbai"
NAVAREA_AUTHORITY     = "NAVAREA VIII Coordinator (NHO India)"
MOEF_AUTHORITY        = "Ministry of Environment, Forest and Climate Change (MoEFCC)"
FISHERIES_AUTHORITY   = "Maharashtra Marine Fisheries Department"


def _coord_str(lat: float, lon: float) -> str:
    lat_ch = "N" if lat >= 0 else "S"
    lon_ch = "E" if lon >= 0 else "W"
    return f"{abs(lat):.4f}°{lat_ch}, {abs(lon):.4f}°{lon_ch}"


def _vessel_description(mmsi: str, vessel_type: str) -> str:
    if mmsi and mmsi not in ("", "None"):
        return f"MMSI {mmsi} ({vessel_type or 'unknown type'})"
    return f"unidentified vessel ({vessel_type or 'unknown type'})"


def generate_recommendations(
    spill_id: str,
    spill_lat: float,
    spill_lon: float,
    severity_level: str,
    severity_score: float,
    protected_area_risk: float,
    population_risk: float,
    protected_areas_nearby: int,
    coast_distance_m: float,
    top_vessel_mmsi: str,
    top_vessel_type: str,
    top_attribution_score: float,
    top_attribution_label: str,
    forecast_3h_lat: Optional[float],
    forecast_3h_lon: Optional[float],
    is_synthetic: bool,
) -> List[Dict[str, Any]]:
    priority = TIER_PRIORITY.get(severity_level, "MEDIUM")
    coord = _coord_str(spill_lat, spill_lon)
    vessel_desc = _vessel_description(top_vessel_mmsi, top_vessel_type)
    recs = []

    def add(text: str, p: str = priority, rationale: str = ""):
        recs.append({"recommendation": text, "priority": p, "rationale": rationale})

    # Always: spill alert
    add(
        f"Issue oil spill alert for SAR-detected slick at {coord}. "
        f"Severity score={severity_score:.2f} (tier={severity_level}). "
        f"Notify {COAST_GUARD_AUTHORITY}.",
        rationale="SAR detection confirmed as possible_oil_spill by GLCM texture analysis.",
    )

    # Severity-specific immediate actions
    if severity_level == "CRITICAL":
        add(
            f"IMMEDIATE: Activate full Marine Pollution Response. Deploy {MARINE_POLLUTION_TEAM} "
            f"to spill centroid at {coord}. Issue NAVAREA VIII warning via {NAVAREA_AUTHORITY}.",
            p="URGENT",
            rationale=f"CRITICAL severity (score={severity_score:.2f}).",
        )
        add(
            f"Alert {MOEF_AUTHORITY} and State Disaster Management Authority. "
            f"Initiate environmental impact assessment for spill at {coord}.",
            p="URGENT",
            rationale="CRITICAL spill requires regulatory notification within 1 hour.",
        )
    elif severity_level == "HIGH":
        add(
            f"Deploy containment boom and skimming equipment to {coord} within 2 hours. "
            f"Coordinate with {MARINE_POLLUTION_TEAM}.",
            p="HIGH",
            rationale=f"HIGH severity (score={severity_score:.2f}).",
        )

    # Protected area proximity
    if protected_areas_nearby > 0 and protected_area_risk > 0.3:
        add(
            f"ALERT: {protected_areas_nearby} protected area(s) within 10 km of spill at {coord}. "
            f"Deploy protective booms around sensitive habitats. "
            f"Notify {MOEF_AUTHORITY} of potential Marine Protected Area impact.",
            p="URGENT" if protected_area_risk > 0.7 else "HIGH",
            rationale=f"Protected area risk score={protected_area_risk:.2f}.",
        )

    # Coastal population risk
    if population_risk > 0.5:
        coast_km = coast_distance_m / 1000.0
        add(
            f"Coastal advisory: spill is {coast_km:.1f} km from coastline at {coord}. "
            f"Notify coastal communities of potential shoreline oiling. "
            f"Activate shoreline response teams.",
            p="HIGH" if population_risk > 0.7 else "MEDIUM",
            rationale=f"Population risk score={population_risk:.2f}.",
        )

    # Vessel attribution
    if top_attribution_label in ("probable_source", "possible_source") and top_attribution_score > 0.3:
        conf_word = "probable" if top_attribution_label == "probable_source" else "possible"
        add(
            f"Intercept and inspect {vessel_desc} identified as {conf_word} pollution source "
            f"(attribution score={top_attribution_score:.2f}). "
            f"Request AIS log and cargo manifest. Coordinate with port state control.",
            p="HIGH" if top_attribution_label == "probable_source" else "MEDIUM",
            rationale=f"Source attribution: {top_attribution_label} (score={top_attribution_score:.2f}).",
        )
        if top_vessel_type and "tanker" in top_vessel_type.lower():
            add(
                f"Tanker {vessel_desc} flagged as {conf_word} source. "
                f"Initiate MARPOL violation report. Retain vessel for inspection. "
                f"Notify Flag State and nearest port authority.",
                p="URGENT" if top_attribution_label == "probable_source" else "HIGH",
                rationale="Tankers carry MARPOL obligations; probable tanker discharge triggers mandatory reporting.",
            )

    # Drift trajectory
    if forecast_3h_lat is not None and forecast_3h_lon is not None:
        fc_coord = _coord_str(forecast_3h_lat, forecast_3h_lon)
        add(
            f"Spill drift forecast: slick predicted to reach {fc_coord} within 3 hours. "
            f"Pre-position containment assets at {fc_coord}. "
            f"Alert {COAST_GUARD_AUTHORITY} of forecast trajectory.",
            p="HIGH" if severity_level in ("CRITICAL", "HIGH") else "MEDIUM",
            rationale="Based on 3%-wind + 100%-current Lagrangian drift model.",
        )

    # Fisheries
    if population_risk > 0.3 or protected_area_risk > 0.3:
        add(
            f"Issue fishing ban advisory within 10 km of {coord}. "
            f"Notify {FISHERIES_AUTHORITY} of potential fishery impact.",
            p="MEDIUM",
            rationale="Oil contamination poses risk to marine food chain.",
        )

    # Ongoing monitoring
    add(
        f"Schedule follow-up SAR tasking over {coord} within 6 hours to track spill evolution. "
        f"Continue AIS monitoring for vessels in the area.",
        p="MEDIUM",
        rationale="Ongoing monitoring required to track drift, area change, and new vessel activity.",
    )

    if is_synthetic:
        for r in recs:
            r["recommendation"] = "[SYNTHETIC DEMO] " + r["recommendation"]

    return recs
