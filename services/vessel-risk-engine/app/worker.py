"""Vessel Risk Engine background worker — consumes anomaly.events, ais.trust, sts.events."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/app")

from shared.db import get_pool
from shared.redis_client import get_redis, ensure_consumer_group, consume_stream, publish_to_stream
from app.scorer import compute_risk_score

log = logging.getLogger(__name__)

CONSUMER_GROUP = "risk-engine"
SIGNAL_STREAMS = ["anomaly.events", "ais.trust", "sts.events"]

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "signals_processed": 0,
    "scores_updated": 0,
}


async def run_risk_worker() -> None:
    pool = await get_pool()
    redis = await get_redis()
    for stream in SIGNAL_STREAMS:
        await ensure_consumer_group(redis, stream, CONSUMER_GROUP)
    log.info("Vessel Risk Engine worker started.")

    while True:
        STATE["heartbeat"] = time.time()
        for stream in SIGNAL_STREAMS:
            consumer_name = f"risk-worker-{stream.replace('.', '-')}"
            messages = await consume_stream(
                redis, stream, CONSUMER_GROUP, consumer_name,
                count=50, block_ms=200,
            )
            for msg in messages:
                mmsi_raw = msg["data"].get("mmsi")
                if mmsi_raw:
                    try:
                        await _recompute_risk(int(float(mmsi_raw)), pool, redis)
                        STATE["signals_processed"] += 1
                    except Exception as exc:
                        log.warning("Risk compute error MMSI %s: %s", mmsi_raw, exc)
        await asyncio.sleep(0.05)


async def _recompute_risk(mmsi: int, pool, redis) -> None:
    """Full risk score recomputation for one vessel from current DB state."""
    vessel_row = await pool.fetchrow(
        "SELECT vessel_type, last_seen FROM vessels WHERE mmsi=$1", mmsi
    )
    if not vessel_row:
        return

    vessel_type = (vessel_row["vessel_type"] or "unknown").lower()
    last_seen = vessel_row["last_seen"]
    if last_seen:
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        hours_since = (datetime.now(timezone.utc) - last_seen).total_seconds() / 3600.0
    else:
        hours_since = 0.0

    # Recent anomaly events (last 6h)
    anomaly_rows = await pool.fetch(
        "SELECT anomaly_type, severity FROM anomaly_events "
        "WHERE mmsi=$1 AND window_start >= NOW() - INTERVAL '6 hours'",
        mmsi,
    )
    anomaly_events = [dict(r) for r in anomaly_rows]

    # Trust score (latest)
    trust_row = await pool.fetchrow(
        "SELECT rolling_trust_score FROM ais_trust_scores "
        "WHERE mmsi=$1 ORDER BY timestamp DESC LIMIT 1",
        mmsi,
    )
    trust_score = float(trust_row["rolling_trust_score"]) if trust_row and trust_row["rolling_trust_score"] else 1.0

    # Dark vessel flag — check dark_vessel_events table near vessel's last position
    dark_flag = False
    try:
        dark_row = await pool.fetchrow(
            """SELECT id FROM dark_vessel_events
               WHERE timestamp >= NOW() - INTERVAL '6 hours'
                 AND ST_DWithin(
                     geom,
                     (SELECT ST_SetSRID(ST_MakePoint(last_lon, last_lat), 4326)::geography
                      FROM vessels WHERE mmsi=$1 AND last_lat IS NOT NULL LIMIT 1),
                     10000
                 ) LIMIT 1""",
            mmsi,
        )
        dark_flag = dark_row is not None
    except Exception:
        dark_flag = False  # dark_vessel_events table may not exist yet (SAR phase)

    # STS events (last 48h)
    sts_rows = await pool.fetch(
        "SELECT vessel_a, vessel_b, start_time, end_time FROM sts_events "
        "WHERE (vessel_a=$1 OR vessel_b=$1) AND start_time >= NOW() - INTERVAL '48 hours'",
        mmsi,
    )
    sts_events = [
        {
            "vessel_a": r["vessel_a"], "vessel_b": r["vessel_b"],
            "start_time": r["start_time"].isoformat() if r["start_time"] else None,
            "end_time": r["end_time"].isoformat() if r["end_time"] else None,
        }
        for r in sts_rows
    ]

    # Previous risk score for decay
    prev_row = await pool.fetchrow("SELECT risk_score, tier FROM vessel_risk_scores WHERE mmsi=$1", mmsi)
    prev_score = float(prev_row["risk_score"]) if prev_row and prev_row["risk_score"] is not None else None
    old_tier = prev_row["tier"] if prev_row else None

    result = compute_risk_score(
        mmsi, anomaly_events, trust_score, dark_flag, sts_events,
        vessel_type, prev_score, hours_since,
    )

    # Upsert risk score
    await pool.execute(
        """INSERT INTO vessel_risk_scores
               (mmsi, risk_score, tier, contributing_factors, recommended_action, updated_at)
           VALUES ($1,$2,$3,$4::jsonb,$5,$6)
           ON CONFLICT (mmsi) DO UPDATE SET
               risk_score=EXCLUDED.risk_score,
               tier=EXCLUDED.tier,
               contributing_factors=EXCLUDED.contributing_factors,
               recommended_action=EXCLUDED.recommended_action,
               updated_at=EXCLUDED.updated_at""",
        mmsi, result["risk_score"], result["tier"],
        json.dumps(result["contributing_factors"]),
        result["recommended_action"],
        datetime.now(timezone.utc),
    )
    STATE["scores_updated"] += 1

    # Publish if tier changed or score is notable
    if result["tier"] != old_tier or result["tier"] in ("MEDIUM", "HIGH", "CRITICAL"):
        try:
            await publish_to_stream(redis, "vessel.risk", {
                "mmsi": mmsi,
                "risk_score": result["risk_score"],
                "tier": result["tier"],
                "recommended_action": result["recommended_action"],
            })
        except Exception as e:
            log.error("Publish vessel.risk MMSI %d: %s", mmsi, e)

    # Issue satellite tasking request for HIGH/CRITICAL
    if result["tier"] in ("HIGH", "CRITICAL"):
        try:
            await pool.execute(
                """INSERT INTO satellite_tasking_requests
                       (mmsi, risk_score, risk_tier, reason)
                   VALUES ($1,$2,$3,$4::jsonb)""",
                mmsi, result["risk_score"], result["tier"],
                json.dumps({"contributing_factors": result["contributing_factors"]}),
            )
            log.warning(
                "SATELLITE TASKING: MMSI=%d tier=%s score=%.1f",
                mmsi, result["tier"], result["risk_score"],
            )
        except Exception as e:
            log.error("satellite_tasking_requests insert MMSI %d: %s", mmsi, e)
