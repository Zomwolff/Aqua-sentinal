"""
Severity Impact worker — consumes spill.attributed, queries PostGIS for
protected areas and coastline proximity, computes severity, persists to
severity table, publishes to spill.severity.
"""
from __future__ import annotations
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/app")
from shared.db.connection import create_pool
from shared.redis_client import (
    consume_stream, ensure_consumer_group,
    get_redis, publish_to_stream,
)
from app.severity import compute_severity, SEVERITY_PROTECTED_RADIUS_M, SEVERITY_COAST_RADIUS_M

log = logging.getLogger(__name__)

SERVICE_NAME   = "severity-impact"
CONSUMER_GROUP = "severity-impact"
CONSUMER_NAME  = "si-worker"
INPUT_STREAM   = "spill.attributed"
OUTPUT_STREAM  = "spill.severity"

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "spills_processed": 0,
    "failed": 0,
    "last_spill_id": None,
}


def _parse_float(val, default=0.0):
    try:
        return float(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


async def _fetch_spill_area(pool, spill_id: str) -> float:
    row = await pool.fetchrow(
        "SELECT area_km2 FROM spill_incidents WHERE id = $1", spill_id
    )
    if row and row["area_km2"]:
        return float(row["area_km2"]) * 1_000_000.0  # km2 to m2
    return 500_000.0  # 0.5 km2 fallback


async def _fetch_nearby_protected_areas(
    pool, spill_lat: float, spill_lon: float, radius_m: float
) -> List[Dict]:
    """
    Find all protected_areas within radius_m of the spill centroid.
    Uses PostGIS geography ST_DWithin.
    """
    rows = await pool.fetch(
        """
        SELECT name, area_type,
               ST_Distance(
                   geom::geography,
                   ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography
               ) AS distance_m
        FROM protected_areas
        WHERE ST_DWithin(
            geom::geography,
            ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography,
            $3
        )
        ORDER BY distance_m ASC
        """,
        spill_lat, spill_lon, radius_m,
    )
    return [{"name": r["name"], "area_type": r["area_type"],
             "distance_m": float(r["distance_m"])} for r in rows]


async def _fetch_coast_distance(
    pool, spill_lat: float, spill_lon: float
) -> float:
    """
    Closest coastline distance using reference_layers (layer_type='coastline').
    Falls back to SEVERITY_COAST_RADIUS_M if no coastline data exists.
    """
    row = await pool.fetchrow(
        """
        SELECT ST_Distance(
            geom::geography,
            ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography
        ) AS dist_m
        FROM reference_layers
        WHERE layer_type = 'coastline'
        ORDER BY dist_m ASC
        LIMIT 1
        """,
        spill_lat, spill_lon,
    )
    if row and row["dist_m"] is not None:
        return float(row["dist_m"])
    return float(SEVERITY_COAST_RADIUS_M)  # no coastline data — assume offshore


async def _process_spill_attributed(
    data: Dict[str, Any], pool, redis,
) -> None:
    spill_id  = str(data.get("spill_id") or "")
    spill_lat = _parse_float(data.get("spill_lat"))
    spill_lon = _parse_float(data.get("spill_lon"))
    is_synthetic = str(data.get("is_synthetic", "")).lower() in ("true", "1")

    if not spill_id:
        log.warning("spill.attributed missing spill_id")
        return

    area_m2 = await _fetch_spill_area(pool, spill_id)
    protected = await _fetch_nearby_protected_areas(
        pool, spill_lat, spill_lon, SEVERITY_PROTECTED_RADIUS_M
    )
    coast_dist = await _fetch_coast_distance(pool, spill_lat, spill_lon)

    result = compute_severity(area_m2, protected, coast_dist)

    try:
        await pool.execute(
            """
            INSERT INTO severity
                (spill_id, severity_level, score, environmental_risk,
                 population_risk, economic_risk, protected_area_risk, computed_at)
            VALUES ($1, $2::severity_level_enum, $3, $4, $5, $6, $7, NOW())
            """,
            spill_id,
            result["severity_level"],
            result["score"],
            result["environmental_risk"],
            result["population_risk"],
            result["area_score"],      # economic_risk proxy: area
            result["protected_area_risk"],
        )
    except Exception as e:
        log.error("severity insert failed spill=%s: %s", spill_id, e)

    await publish_to_stream(redis, OUTPUT_STREAM, {
        "spill_id": spill_id,
        "spill_lat": spill_lat,
        "spill_lon": spill_lon,
        "severity_level": result["severity_level"],
        "severity_score": result["score"],
        "protected_area_risk": result["protected_area_risk"],
        "population_risk": result["population_risk"],
        "environmental_risk": result["environmental_risk"],
        "protected_areas_nearby": result["protected_areas_nearby"],
        "coast_distance_m": result["coast_distance_m"],
        "is_synthetic": is_synthetic,
    })

    STATE["spills_processed"] += 1
    STATE["last_spill_id"] = spill_id
    log.info(
        "Severity: spill=%s tier=%s score=%.3f protected=%d coast_dist=%.0fm",
        spill_id, result["severity_level"], result["score"],
        result["protected_areas_nearby"], coast_dist,
    )


async def run_severity_worker() -> None:
    pool  = await create_pool()
    redis = await get_redis()
    await ensure_consumer_group(redis, INPUT_STREAM, CONSUMER_GROUP)
    log.info("%s worker started.", SERVICE_NAME)

    while True:
        STATE["heartbeat"] = time.time()
        messages = await consume_stream(
            redis, INPUT_STREAM, CONSUMER_GROUP, CONSUMER_NAME,
            count=10, block_ms=2000,
        )
        for msg in messages:
            try:
                await _process_spill_attributed(msg["data"], pool, redis)
            except Exception as exc:
                STATE["failed"] += 1
                log.exception("Severity failed msg=%s: %s", msg.get("id"), exc)
