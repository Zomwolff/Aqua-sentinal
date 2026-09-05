"""STS Detection background worker — consumes ais.features, runs state machine."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict

sys.path.insert(0, "/app")

from shared.db import get_pool
from shared.redis_client import get_redis, ensure_consumer_group, consume_stream, publish_to_stream
from app.state_machine import update_pair, close_stale_pairs, STS_DISTANCE_THRESHOLD_M

log = logging.getLogger(__name__)

CONSUMER_GROUP = "sts-detection"
CONSUMER_NAME = "sts-worker"
STALE_CHECK_INTERVAL_S = 60

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "features_consumed": 0,
    "sts_events_emitted": 0,
}


async def run_sts_worker() -> None:
    pool = await get_pool()
    redis = await get_redis()
    await ensure_consumer_group(redis, "ais.features", CONSUMER_GROUP)
    log.info("STS Detection worker started.")
    last_stale_check = 0.0

    while True:
        STATE["heartbeat"] = time.time()
        messages = await consume_stream(
            redis, "ais.features", CONSUMER_GROUP, CONSUMER_NAME,
            count=50, block_ms=2000,
        )
        for msg in messages:
            await _process_features(msg["data"], pool, redis)
            STATE["features_consumed"] += 1

        if time.time() - last_stale_check >= STALE_CHECK_INTERVAL_S:
            last_stale_check = time.time()
            await close_stale_pairs(redis, pool)


async def _process_features(features: Dict[str, Any], pool, redis) -> None:
    try:
        mmsi_raw = features.get("mmsi")
        if not mmsi_raw:
            return
        mmsi = int(float(mmsi_raw))

        proximity_raw = features.get("proximity_events")
        if not proximity_raw:
            return

        if isinstance(proximity_raw, str):
            try:
                proximity_events = json.loads(proximity_raw)
            except json.JSONDecodeError:
                return
        else:
            proximity_events = proximity_raw

        if not proximity_events:
            return

        avg_speed_a = float(features.get("avg_speed") or 0)

        vessel_row = await pool.fetchrow(
            "SELECT last_lat, last_lon FROM vessels WHERE mmsi=$1", mmsi
        )
        my_lat = float(vessel_row["last_lat"]) if vessel_row and vessel_row["last_lat"] else 0.0
        my_lon = float(vessel_row["last_lon"]) if vessel_row and vessel_row["last_lon"] else 0.0
        now_ts = datetime.now(timezone.utc)

        for prox in proximity_events:
            other_mmsi = int(prox.get("mmsi", 0))
            if not other_mmsi:
                continue
            distance_m = float(prox.get("distance_m", 99999))
            if distance_m > STS_DISTANCE_THRESHOLD_M:
                continue

            # Fetch other vessel's last speed from features table
            other_row = await pool.fetchrow(
                "SELECT avg_speed FROM vessel_features WHERE mmsi=$1 ORDER BY window_end DESC LIMIT 1",
                other_mmsi,
            )
            avg_speed_b = float(other_row["avg_speed"]) if other_row and other_row["avg_speed"] else 0.0

            event = await update_pair(
                redis, mmsi, other_mmsi,
                distance_m, avg_speed_a, avg_speed_b,
                my_lat, my_lon, now_ts,
            )
            if event:
                await _save_sts_event(event, pool)
                await publish_to_stream(redis, "sts.events", {
                    k: str(v) for k, v in event.items()
                })
                STATE["sts_events_emitted"] += 1
                log.info(
                    "STS event: MMSI %d & %d | dur=%.1fmin dist=%.0fm conf=%.2f",
                    event["vessel_a"], event["vessel_b"],
                    event["duration_minutes"], event["avg_distance_m"], event["confidence"],
                )

    except Exception as exc:
        log.exception("STS processing error MMSI %s: %s", features.get("mmsi"), exc)


async def _save_sts_event(event: Dict[str, Any], pool) -> None:
    try:
        vessel_a_mmsi = str(event["vessel_a"])
        vessel_b_mmsi = str(event["vessel_b"])

        start_dt = datetime.fromisoformat(event["start_time"].replace("Z", "+00:00"))
        end_dt = (datetime.fromisoformat(event["end_time"].replace("Z", "+00:00"))
                  if event.get("end_time") else None)

        # Use proper PostGIS geometry (not geography) to match schema GEOMETRY(Point,4326)
        loc_wkt = (f"POINT({event['centroid_lon']} {event['centroid_lat']})"
                   if event.get("centroid_lat") else None)

        # Look up vessel DB IDs for FK integrity
        row_a = await pool.fetchrow("SELECT id FROM vessels WHERE mmsi=$1", vessel_a_mmsi)
        row_b = await pool.fetchrow("SELECT id FROM vessels WHERE mmsi=$1", vessel_b_mmsi)
        vessel_a_id = row_a["id"] if row_a else None
        vessel_b_id = row_b["id"] if row_b else None

        await pool.execute(
            """INSERT INTO sts_events
               (vessel_a_id, vessel_b_id, vessel_a_mmsi, vessel_b_mmsi,
                start_time, end_time, duration_minutes,
                avg_distance_m, min_distance_m, avg_combined_speed_knots, confidence, location)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                       CASE WHEN $12 IS NOT NULL
                            THEN ST_SetSRID(ST_GeomFromText($12), 4326)
                            ELSE NULL END)
               ON CONFLICT DO NOTHING""",
            vessel_a_id, vessel_b_id, vessel_a_mmsi, vessel_b_mmsi,
            start_dt, end_dt,
            event["duration_minutes"], event["avg_distance_m"], event["min_distance_m"],
            event["avg_combined_speed_knots"], event["confidence"], loc_wkt,
        )
        log.info(
            "Saved STS event: MMSI %s <-> %s | dur=%.1fmin conf=%.2f",
            vessel_a_mmsi, vessel_b_mmsi, event["duration_minutes"], event["confidence"],
        )
    except Exception as e:
        log.error("DB insert sts_events failed: %s | event=%r", e, event)
