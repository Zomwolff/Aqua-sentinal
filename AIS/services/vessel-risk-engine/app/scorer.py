"""
Vessel Risk Scorer — explainable weighted scorecard (no black box).

risk_score = 30 * anomaly_weight
           + 25 * (1 - trust_score)
           + 25 * dark_vessel_flag
           + 20 * sts_weight
=> [0, 100]

Tier:  < 20 = LOW  |  20-50 = MEDIUM  |  50-75 = HIGH  |  > 75 = CRITICAL
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

RISK_TIER_MEDIUM   = float(os.environ.get("RISK_TIER_MEDIUM", 20))
RISK_TIER_HIGH     = float(os.environ.get("RISK_TIER_HIGH", 50))
RISK_TIER_CRITICAL = float(os.environ.get("RISK_TIER_CRITICAL", 75))
RISK_DECAY_RATE    = float(os.environ.get("RISK_DECAY_RATE", 0.05))  # fraction per hour


def _anomaly_weight(events: List[Dict[str, Any]], weather_stats: Optional[Dict[str, Any]] = None) -> Tuple[float, str, float]:
    if not events:
        return 0.0, "no_anomalies", 1.0

    raw_weight = 0.5
    desc = f"{len(events)}_low_severity_anomaly"
    sevs = [e.get("severity", "LOW") for e in events]
    if "HIGH" in sevs:
        raw_weight = 1.0
        desc = f"{sevs.count('HIGH')}_high_severity_anomaly"
    elif "MEDIUM" in sevs:
        raw_weight = 0.8
        desc = f"{sevs.count('MEDIUM')}_medium_severity_anomaly"

    # Weather forgiveness: High wind speeds cause erratic navigational behaviour
    weather_multiplier = 1.0
    if weather_stats:
        wind_speed = weather_stats.get("wind_speed_kmh", 0.0) or 0.0
        if wind_speed > 40:
            weather_multiplier = 0.4  # 60% reduction
            desc += " (severe_weather_forgiveness)"
        elif wind_speed > 25:
            weather_multiplier = 0.7  # 30% reduction
            desc += " (rough_weather_forgiveness)"

    return raw_weight * weather_multiplier, desc, weather_multiplier


def _sts_weight(events: List[Dict[str, Any]], vessel_type: str = "unknown") -> Tuple[float, str]:
    if not events:
        return 0.0, "no_sts_events"
    active = [e for e in events if e.get("end_time") is None]
    if active:
        return (1.0, "active_sts_tanker") if vessel_type == "tanker" else (0.8, "active_sts_event")
    now = datetime.now(timezone.utc)
    recent = []
    for e in events:
        end_raw = e.get("end_time")
        if end_raw:
            try:
                end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                if now - end_dt < timedelta(hours=24):
                    recent.append(e)
            except (ValueError, TypeError):
                pass
    if recent:
        return 0.5, f"{len(recent)}_sts_event_last_24h"
    return 0.3, "past_sts_events_only"


def compute_risk_score(
    mmsi: int,
    anomaly_events: List[Dict[str, Any]],
    trust_score: float,
    dark_vessel_flag: bool,
    sts_events: List[Dict[str, Any]],
    vessel_type: str = "unknown",
    previous_risk_score: Optional[float] = None,
    hours_since_last_signal: float = 0.0,
    weather_stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute full risk score with factor breakdown."""
    anomaly_w, anomaly_desc, weather_mult = _anomaly_weight(anomaly_events, weather_stats)
    sts_w, sts_desc = _sts_weight(sts_events, vessel_type)

    anomaly_c = 30.0 * anomaly_w
    trust_c   = 25.0 * (1.0 - max(0.0, min(1.0, trust_score)))
    dark_c    = 25.0 if dark_vessel_flag else 0.0
    sts_c     = 20.0 * sts_w

    raw_score = anomaly_c + trust_c + dark_c + sts_c

    # Apply time-decay on the previous score (new signal wins if higher)
    if previous_risk_score is not None and hours_since_last_signal > 0:
        decay_factor = max(0.0, 1.0 - RISK_DECAY_RATE * hours_since_last_signal)
        raw_score = max(raw_score, previous_risk_score * decay_factor)

    risk_score = round(min(100.0, max(0.0, raw_score)), 2)

    if risk_score >= RISK_TIER_CRITICAL:
        tier, action = "CRITICAL", "satellite_task+alert_authorities"
    elif risk_score >= RISK_TIER_HIGH:
        tier, action = "HIGH", "satellite_task"
    elif risk_score >= RISK_TIER_MEDIUM:
        tier, action = "MEDIUM", "monitor+flag"
    else:
        tier, action = "LOW", "monitor"

    factors = [
        {"factor": "anomaly",      "weight": 30, "value": round(anomaly_w, 3), "contribution": round(anomaly_c, 2), "description": anomaly_desc},
        {"factor": "trust",        "weight": 25, "value": round(1.0 - trust_score, 3), "contribution": round(trust_c, 2), "description": f"trust={trust_score:.3f}"},
        {"factor": "dark_vessel",  "weight": 25, "value": 1.0 if dark_vessel_flag else 0.0, "contribution": round(dark_c, 2), "description": "dark_vessel" if dark_vessel_flag else "not_dark"},
        {"factor": "sts",          "weight": 20, "value": round(sts_w, 3), "contribution": round(sts_c, 2), "description": sts_desc},
    ]

    if weather_mult < 1.0:
        # Show exactly how much score was shaved off due to weather forgiveness
        # raw_anomaly_c would have been 30.0 * (anomaly_w / weather_mult)
        raw_anomaly_w = anomaly_w / weather_mult
        discount_c = 30.0 * raw_anomaly_w - anomaly_c
        factors.append({
            "factor": "weather_forgiveness",
            "weight": 0,
            "value": round(weather_mult, 3),
            "contribution": -round(discount_c, 2),
            "description": "score_discounted_due_to_severe_weather"
        })

    return {
        "mmsi": mmsi,
        "risk_score": risk_score,
        "tier": tier,
        "contributing_factors": factors,
        "recommended_action": action,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def score_to_tier(risk_score: float) -> str:
    if risk_score >= RISK_TIER_CRITICAL:
        return "CRITICAL"
    elif risk_score >= RISK_TIER_HIGH:
        return "HIGH"
    elif risk_score >= RISK_TIER_MEDIUM:
        return "MEDIUM"
    return "LOW"
