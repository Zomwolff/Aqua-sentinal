"""
AIS Analytics background worker.

Consumes ais.clean stream → buffers pings per vessel → flushes windows every
WINDOW_MINUTES → computes behavioral features → writes to vessel_features table
→ publishes to ais.features stream.

Design decisions:
- Window flush is time-based (wall clock), not message-count-based.
  This ensures features are published even if a vessel sends few pings.
- Cross-vessel proximity check uses all active vessel positions from
  Postgres last_lat/last_lon (cached in vessels table by ingestion service).
- Welford's online algorithm state for anomaly detection is NOT maintained
  here — that belongs to the anomaly-detection service which owns its own
  per-vessel history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/app")

from shared.db import get_pool
from shared.redis_client import get_redis, ensure_consumer_group, consume_stream, publish_to_stream
from app.window_buffer import WindowBuffer, WINDOW_MINUTES
from app.feature_extractor import compute_features
from shared.db import get_all_active_vessels

log = logging.getLogger(__name__)

SERVICE_NAME = "ais-analytics"
CONSUMER_GROUP = "analytics"
CONSUMER_NAME = "ais-analytics-worker"

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "messages_consumed": 0,
    "windows_flushed": 0,
    "last_flush_at": None,
}

# Track when each vessel's window was last flushed: {mmsi: datetime}
_last_flush: Dict[int, datetime] = {}


async def run_analytics_worker() -> None:
    """Main analytics worker loop."""
    pool = await get_pool()
    redis = await get_redis()
    buffer = WindowBuffer(redis)

    await ensure_consumer_group(redis, "ais.clean", CONSUMER_GROUP)
    log.info("%s worker started (window=%dmin)", SERVICE_NAME, WINDOW_MINUTES)

    window_duration = timedelta(minutes=WINDOW_MINUTES)
    flush_check_interval = 30  # check for flushable windows every 30s
    last_flush_check = time.time()

    while True:
        STATE["heartbeat"] = time.time()

        # ── Consume new pings from ais.clean ──────────────────────────────────
        messages = await consume_stream(
            redis, "ais.clean", CONSUMER_GROUP, CONSUMER_NAME,
            count=200, block_ms=1000,
        )

        for msg in messages:
            data = msg["data"]
            try:
                mmsi = int(data["mmsi"])
            except (KeyError, ValueError, TypeError):
                continue

            # Reconstruct the ping dict
            ping = {k: v for k, v in data.items()}

            await buffer.add_ping(mmsi, ping)
            STATE["messages_consumed"] += 1

            # Initialise flush tracker for new vessels
            if mmsi not in _last_flush:
                _last_flush[mmsi] = datetime.now(timezone.utc)

        # ── Periodic window flush check ───────────────────────────────────────
        now = time.time()
        if now - last_flush_check >= flush_check_interval:
            last_flush_check = now
            now_dt = datetime.now(timezone.utc)

            # Get all active vessels for cross-proximity check
            try:
                active_vessels = await get_all_active_vessels(pool, active_within_minutes=WINDOW_MINUTES + 5)
            except Exception as e:
                log.warning("Failed to fetch active vessels for proximity: %s", e)
                active_vessels = []

            for mmsi, last_flush_dt in list(_last_flush.items()):
                if now_dt - last_flush_dt >= window_duration:
                    await _flush_window(mmsi, buffer, pool, redis, active_vessels, now_dt)

        await asyncio.sleep(0.01)  # yield to event loop


async def _flush_window(
    mmsi: int,
    buffer: WindowBuffer,
    pool,
    redis,
    active_vessels: List[Dict[str, Any]],
    now_dt: datetime,
) -> None:
    """
    Flush the current window for a vessel: compute features, write to DB, publish.
    """
    window_end = now_dt
    window_start = window_end - timedelta(minutes=WINDOW_MINUTES)

    pings = await buffer.get_window(
        mmsi,
        window_start=window_start.timestamp(),
        window_end=window_end.timestamp(),
    )

    if not pings:
        # No pings in this window — vessel may have gone quiet
        # Don't flush an empty window; the anomaly detector will catch AIS gaps separately
        _last_flush[mmsi] = now_dt
        return

    # Check port proximity using the last known position
    last_ping = max(pings, key=lambda p: p.get("ts_epoch", 0))
    last_lat = last_ping.get("lat")
    last_lon = last_ping.get("lon")
    port_nearby = False
    near_port_name = None
    if last_lat is not None and last_lon is not None:
        try:
            from app.reference_loader_cache import is_near_port
            near_port_name = is_near_port(float(last_lat), float(last_lon))
            port_nearby = near_port_name is not None
        except ImportError:
            pass  # reference_loader_cache is optional; port check degrades gracefully

    features = compute_features(
        mmsi=mmsi,
        pings=pings,
        window_start=window_start,
        window_end=window_end,
        port_nearby=port_nearby,
        all_vessel_positions=active_vessels,
    )

    # ── Write to vessel_features table ────────────────────────────────────────
    try:
        vessel_row = await pool.fetchrow("SELECT id FROM vessels WHERE mmsi=$1", str(mmsi))
        if not vessel_row:
            log.warning("Feature window skipped: vessel %s no longer exists.", mmsi)
            return
        await pool.execute(
            """
            INSERT INTO vessel_features
                (mmsi, window_start, window_end, ping_count, avg_speed, speed_variance,
                 max_speed, course_variance, heading_change_rate, loitering_score,
                 distance_traveled_km, proximity_events, quality, vessel_id,
                 max_speed_delta_kn, max_speed_drop_kn,
                 mean_cog_heading_divergence_deg, max_cog_heading_divergence_deg,
                 repeated_cog_heading_divergences, max_rate_of_turn_deg_min,
                 turn_reversal_count, draught_m, draught_change_m)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
            """,
            str(mmsi),
            window_start,
            window_end,
            features["ping_count"],
            features["avg_speed"],
            features["speed_variance"],
            features["max_speed"],
            features["course_variance"],
            features["heading_change_rate"],
            features["loitering_score"],
            features["distance_traveled_km"],
            json.dumps(features["proximity_events"]),
            features["quality"],
            vessel_row["id"],
            features["max_speed_delta_kn"],
            features["max_speed_drop_kn"],
            features["mean_cog_heading_divergence_deg"],
            features["max_cog_heading_divergence_deg"],
            features["repeated_cog_heading_divergences"],
            features["max_rate_of_turn_deg_min"],
            features["turn_reversal_count"],
            features["draught_m"],
            features["draught_change_m"],
        )
    except Exception as e:
        raise e # for MMSI %d: %s", mmsi, e)

    # ── Publish to ais.features stream ────────────────────────────────────────
    try:
        await publish_to_stream(redis, "ais.features", features)
    except Exception as e:
        log.error("Publish ais.features failed for MMSI %d: %s", mmsi, e)

    _last_flush[mmsi] = now_dt
    STATE["windows_flushed"] += 1
    STATE["last_flush_at"] = now_dt.isoformat()
    log.info(
        "Window flushed: MMSI=%d pings=%d avg_speed=%.1f loitering=%.2f quality=%s",
        mmsi, features["ping_count"],
        features["avg_speed"] or 0,
        features["loitering_score"] or 0,
        features["quality"],
    )
