"""Build presentation-ready spill intelligence from persisted evidence."""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Dict, List, Optional


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    number = float(value) if isinstance(value, (Decimal, int, float)) else float(str(value))
    return number if math.isfinite(number) else None


def _percent(value: Any) -> Optional[int]:
    number = _number(value)
    if number is None:
        return None
    return int(round(max(0.0, min(1.0, number)) * 100.0))


def _risk_level(score: Any) -> str:
    value = _number(score)
    if value is None:
        return "UNKNOWN"
    if value >= 0.80:
        return "CRITICAL"
    if value >= 0.60:
        return "HIGH"
    if value >= 0.30:
        return "MODERATE"
    return "LOW"


def _rounded_shares(weights: List[float]) -> List[int]:
    """Largest-remainder rounding so displayed shares total exactly 100%."""
    total = sum(weights)
    if total <= 0:
        return [0] * len(weights)
    exact = [weight * 100.0 / total for weight in weights]
    result = [math.floor(value) for value in exact]
    for index in sorted(
        range(len(exact)), key=lambda i: exact[i] - result[i], reverse=True
    )[: 100 - sum(result)]:
        result[index] += 1
    return result


def source_distribution(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Create a relative evidence distribution plus an Unknown residual.

    ``final_score`` is an evidential score, not a calibrated probability. When
    scores total less than one, the remainder is deliberately assigned to
    Unknown. Larger totals are normalized. The response labels this distinction.
    """
    sources: List[Dict[str, Any]] = []
    weights: List[float] = []
    for row in rows:
        score = max(0.0, min(1.0, _number(row.get("final_score")) or 0.0))
        weights.append(score)
        sources.append({
            "mmsi": str(row["mmsi"]) if row.get("mmsi") is not None else None,
            "vessel_name": row.get("vessel_name") or "Unnamed vessel",
            "vessel_type": row.get("vessel_type") or "unknown",
            "evidence_score_percent": _percent(score),
            "classification": (
                "probable_source" if score >= 0.75 else
                "possible_source" if score >= 0.50 else
                "correlated" if score >= 0.25 else
                "insufficient_evidence"
            ),
        })

    weights.append(max(0.0, 1.0 - sum(weights)))
    shares = _rounded_shares(weights)
    for rank, source in enumerate(sources, start=1):
        source["rank"] = rank
        source["relative_likelihood_percent"] = shares[rank - 1]

    sources.append({
        "rank": None,
        "mmsi": None,
        "vessel_name": "Unknown",
        "vessel_type": None,
        "evidence_score_percent": None,
        "classification": "unattributed_residual",
        "relative_likelihood_percent": shares[-1] if shares else 100,
    })
    return sources


def build_incident_report(
    incident: Dict[str, Any],
    severity: Optional[Dict[str, Any]],
    attribution: List[Dict[str, Any]],
    forecasts: List[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    severity = severity or {}
    environmental = _number(severity.get("environmental_risk"))
    protected = _number(severity.get("protected_area_risk"))
    population = _number(severity.get("population_risk"))
    ecological_score = max(
        (value for value in (environmental, protected, population) if value is not None),
        default=None,
    )

    forecast_points = [{
        "horizon_hours": _number(row.get("horizon_hours")),
        "forecast_time": row.get("forecast_time"),
        "center": {
            "latitude": _number(row.get("predicted_lat")),
            "longitude": _number(row.get("predicted_lon")),
        },
        "confidence_percent": _percent(row.get("confidence")),
        "geometry": row.get("geometry"),
        "model_version": row.get("model_version"),
    } for row in forecasts]
    
    # V1: score is already 0-100, no conversion needed
    severity_score_value = _number(severity.get("score"))
    severity_score_percent = int(round(severity_score_value)) if severity_score_value is not None else None

    return {
        "title": "OIL SPILL INCIDENT",
        "incident_id": str(incident.get("id")),
        "observed_at": incident.get("detected_at"),
        "location": {
            "latitude": _number(incident.get("latitude")),
            "longitude": _number(incident.get("longitude")),
        },
        "assessment": {
            "severity": severity.get("severity_level") or "UNASSESSED",
            "severity_score_percent": severity_score_percent,  # Already 0-100
            "detection_confidence_percent": _percent(incident.get("confidence")),
            "spill_area_km2": _number(incident.get("area_km2")),
            "ecological_risk": {
                "level": _risk_level(ecological_score),
                "score_percent": _percent(ecological_score),
                "basis": {
                    "environmental_percent": _percent(environmental),
                    "protected_area_percent": _percent(protected),
                    "coastal_population_percent": _percent(population),
                },
            },
        },
        "probable_sources": source_distribution(attribution),
        "spill_forecast": {
            "timeline_hours": [0] + [point["horizon_hours"] for point in forecast_points],
            "points": forecast_points,
        },
        "recommended_actions": [{
            "order": index,
            "action": row.get("recommendation"),
            "priority": row.get("priority"),
            "status": row.get("status"),
        } for index, row in enumerate(recommendations, start=1)],
        "provenance": {
            "source": incident.get("source"),
            "source_image_id": incident.get("source_image_id"),
            "is_synthetic": str(incident.get("source", "")).lower() == "synthetic",
            "attribution_note": (
                "Relative likelihood is normalized from circumstantial evidence scores; "
                "it is not a legal finding or calibrated probability."
            ),
            "missing_values": "null/UNKNOWN values were not fabricated",
        },
    }
