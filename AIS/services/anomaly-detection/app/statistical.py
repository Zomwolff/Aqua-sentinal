"""
Statistical anomaly detection via Welford's online algorithm + z-score flagging.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

Z_SCORE_THRESHOLD = 2.5
MIN_WINDOWS_FOR_STATS = 5

_POPULATION_PRIORS: Dict[str, Dict[str, Tuple[float, float]]] = {
    "tanker":    {"avg_speed": (10.0, 4.0), "course_variance": (500.0, 800.0), "distance_traveled_km": (20.0, 15.0)},
    "cargo":     {"avg_speed": (12.0, 5.0), "course_variance": (600.0, 900.0), "distance_traveled_km": (25.0, 18.0)},
    "fishing":   {"avg_speed": (4.0,  3.0), "course_variance": (3000.0, 2000.0), "distance_traveled_km": (8.0, 6.0)},
    "passenger": {"avg_speed": (15.0, 5.0), "course_variance": (400.0, 600.0), "distance_traveled_km": (30.0, 20.0)},
    "tug":       {"avg_speed": (5.0,  3.0), "course_variance": (2000.0, 1500.0), "distance_traveled_km": (8.0, 5.0)},
    "unknown":   {"avg_speed": (8.0,  6.0), "course_variance": (2000.0, 2000.0), "distance_traveled_km": (15.0, 12.0)},
}

_WELFORD_FIELDS = [
    ("avg_speed",             "n_speed", "mean_speed", "m2_speed"),
    ("course_variance",       "n_cvar",  "mean_cvar",  "m2_cvar"),
    ("distance_traveled_km",  "n_dist",  "mean_dist",  "m2_dist"),
]


def _welford_update(n: float, mean: float, m2: float, value: float) -> Tuple[float, float, float]:
    n += 1
    delta = value - mean
    mean += delta / n
    delta2 = value - mean
    m2 += delta * delta2
    return n, mean, m2


def _welford_std(n: float, m2: float) -> float:
    if n < 2:
        return 0.0
    return math.sqrt(max(0.0, m2 / (n - 1)))


async def update_vessel_stats(redis, mmsi: str, features: Dict[str, Any]) -> None:
    """Update per-vessel Welford state in Redis after each window."""
    key = f"ais:stats:{mmsi}"
    raw = await redis.hgetall(key)

    def _f(val, default=0.0):
        try:
            return float(val) if val is not None else default
        except (ValueError, TypeError):
            return default

    updates: Dict[str, str] = {}
    for feat_key, n_key, mean_key, m2_key in _WELFORD_FIELDS:
        value = features.get(feat_key)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        n, mean, m2 = _f(raw.get(n_key)), _f(raw.get(mean_key)), _f(raw.get(m2_key))
        n, mean, m2 = _welford_update(n, mean, m2, value)
        updates[n_key] = str(n)
        updates[mean_key] = str(mean)
        updates[m2_key] = str(m2)

    updates["window_count"] = str(_f(raw.get("window_count")) + 1)
    updates["vessel_type"] = features.get("vessel_type") or raw.get("vessel_type", "unknown") or "unknown"
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    if updates:
        await redis.hset(key, mapping=updates)
        await redis.expire(key, 86400 * 7)


async def compute_z_score_anomalies(
    redis, features: Dict[str, Any], vessel_type: str = "unknown",
) -> List[Dict[str, Any]]:
    """Compute z-score anomalies using per-vessel history or population priors."""
    mmsi = features.get("mmsi")
    window_start = features.get("window_start")
    events: List[Dict[str, Any]] = []

    key = f"ais:stats:{mmsi}"
    raw = await redis.hgetall(key)
    window_count = int(float(raw.get("window_count", 0)))
    use_population = window_count < MIN_WINDOWS_FOR_STATS
    priors = _POPULATION_PRIORS.get(vessel_type.lower(), _POPULATION_PRIORS["unknown"])

    for feat_key, n_key, mean_key, m2_key in _WELFORD_FIELDS:
        value = features.get(feat_key)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue

        if use_population:
            prior = priors.get(feat_key)
            if prior is None:
                continue
            mean, std = prior
        else:
            n = float(raw.get(n_key, 0))
            mean = float(raw.get(mean_key, 0))
            m2 = float(raw.get(m2_key, 0))
            std = _welford_std(n, m2)

        if std < 1e-6:
            continue

        z = (value - mean) / std
        if abs(z) > Z_SCORE_THRESHOLD:
            direction = "high" if z > 0 else "low"
            severity = "HIGH" if abs(z) > Z_SCORE_THRESHOLD * 1.5 else "MEDIUM"
            events.append({
                "mmsi": mmsi, "window_start": window_start,
                "anomaly_type": f"statistical_{feat_key}_{direction}",
                "severity": severity,
                "source": "statistical" if not use_population else "population_prior",
                "evidence": {
                    "feature": feat_key, "value": round(value, 4),
                    "mean": round(mean, 4), "std": round(std, 4),
                    "z_score": round(z, 3), "threshold": Z_SCORE_THRESHOLD,
                    "window_count": window_count,
                },
            })

    return events


def merge_anomaly_events(
    rule_events: List[Dict[str, Any]], stat_events: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge rule + statistical events, keeping highest severity per (mmsi, type)."""
    _sev = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    best: Dict[str, Dict[str, Any]] = {}
    for event in rule_events + stat_events:
        key = f"{event.get('mmsi')}:{event.get('window_start')}:{event.get('anomaly_type')}"
        existing = best.get(key)
        if existing is None or _sev.get(event.get("severity", "LOW"), 0) > _sev.get(existing.get("severity", "LOW"), 0):
            best[key] = event
    return list(best.values())
