"""
Drift Forecast worker — consumes spill.attributed, runs Lagrangian drift
model, persists forecast polygons to the forecasts table.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

sys.path.insert(0, "/app")
from shared.db.connection import create_pool
from shared.redis_client import (
    consume_stream, ensure_consumer_group,
    get_redis, publish_to_stream,
)
from app.drift import forecast_spill

log = logging.getLogger(__name__)

SERVICE_NAME   = "drift-forecast"
CONSUMER_GROUP = "drift-forecast"
CONSUMER_NAME  = "df-worker"
INPUT_STREAM   = "spill.attributed"
OUTPUT_STREAM  = "spill.forecast"
MODEL_VERSION  = "1.0"

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "spills_processed": 0,
    "forecasts_written": 0,
    "failed": 0,
    "last_spill_id": None,
}


def _parse_float(val, default=0.0):
    try:
        return float(val) if val is not None and val != "" else default
    except (TypeError, ValueError):
        return default


async def _fetch_spill_area(pool, spill_id: str) -> float:
    """Get area_m2 from spill_incidents.area_km2 (converted to m2)."""
    row = await pool.fetchrow(
        "SELECT area_km2 FROM spill_incidents WHERE id = $1",
        spill_id,
    )
    if row and row["area_km2"] is not None:
        return float(row["area_km2"]) * 1000000.0  # km2 → m2
    return 1000000.0  # default: 1 km2


async def _fetch_weather(
    pool, spill_lat: float, spill_lon: float, acq_time: datetime,
) -> Dict[str, float]:
    """Nearest environmental_conditions sample near acquisition time."""
    row = await pool.fetchrow(
        """
        SELECT wind_speed_kmh, wind_direction_deg,
               current_speed_ms, current_direction_deg
        FROM environmental_conditions
        WHERE timestamp BETWEEN $3 - INTERVAL '3 hours' AND $3 + INTERVAL '3 hours'
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint($2, $1), 4326)
        LIMIT 1
        """,
        spill_lat, spill_lon, acq_time,
    )
    if row:
        return {
            "wind_speed_ms":    float((row["wind_speed_kmh"] or 0) / 3.6),
            "wind_dir_deg":     float(row["wind_direction_deg"] or 0),
            "current_speed_ms": float(row["current_speed_ms"] or 0),
            "current_dir_deg":  float(row["current_direction_deg"] or 0),
        }
    return {"wind_speed_ms": 0.5, "wind_dir_deg": 225.0,
            "current_speed_ms": 0.2, "current_dir_deg": 200.0}


async def _process_spill_attributed(
    data: Dict[str, Any], pool, redis,
) -> None:
    spill_id = str(data.get("spill_id") or "")
    if not spill_id:
        log.warning("spill.attributed message missing spill_id")
        return

    spill_lat = _parse_float(data.get("spill_lat"))
    spill_lon = _parse_float(data.get("spill_lon"))
    acq_raw   = data.get("acquisition_time")
    is_synthetic = str(data.get("is_synthetic", "")).lower() in ("true", "1")

    try:
        acq = datetime.fromisoformat(str(acq_raw).replace("Z", "+00:00")) if acq_raw else datetime.now(timezone.utc)
        if acq.tzinfo is None:
            acq = acq.replace(tzinfo=timezone.utc)
    except Exception:
        acq = datetime.now(timezone.utc)

    area_m2 = await _fetch_spill_area(pool, spill_id)
    weather = await _fetch_weather(pool, spill_lat, spill_lon, acq)

    forecasts = forecast_spill(
        spill_lat=spill_lat, spill_lon=spill_lon, area_m2=area_m2,
        wind_speed_ms=weather["wind_speed_ms"], wind_dir_deg=weather["wind_dir_deg"],
        current_speed_ms=weather["current_speed_ms"], current_dir_deg=weather["current_dir_deg"],
    )

    for fc in forecasts:
        from datetime import timedelta
        forecast_time = acq + timedelta(hours=fc["horizon_hours"])
        try:
            await pool.execute(
                """
                INSERT INTO forecasts
                    (spill_id, forecast_time, generated_at, horizon_hours,
                     geom, model_version, confidence)
                VALUES ($1, $2, NOW(), $3,
                        ST_SetSRID(ST_GeomFromText($4), 4326),
                        $5, $6)
                """,
                spill_id, forecast_time, fc["horizon_hours"],
                fc["polygon_wkt"], MODEL_VERSION, fc["confidence"],
            )
            STATE["forecasts_written"] += 1
        except Exception as e:
            log.error("Forecast insert failed spill=%s horizon=%sh: %s",
                      spill_id, fc["horizon_hours"], e)

    STATE["last_spill_id"] = spill_id
    STATE["spills_processed"] += 1
    log.info(
        "Drift forecast: spill=%s %d horizons wind=%.1fms/%.0fdeg current=%.2fms/%.0fdeg",
        spill_id, len(forecasts),
        weather["wind_speed_ms"], weather["wind_dir_deg"],
        weather["current_speed_ms"], weather["current_dir_deg"],
    )

    await publish_to_stream(redis, OUTPUT_STREAM, {
        "spill_id": spill_id,
        "forecast_count": len(forecasts),
        "max_horizon_hours": max(f["horizon_hours"] for f in forecasts),
        "wind_speed_ms": weather["wind_speed_ms"],
        "wind_dir_deg": weather["wind_dir_deg"],
        "current_speed_ms": weather["current_speed_ms"],
        "current_dir_deg": weather["current_dir_deg"],
        "is_synthetic": is_synthetic,
    })


async def run_drift_worker() -> None:
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
                log.exception("Drift forecast failed msg=%s: %s", msg.get("id"), exc)
