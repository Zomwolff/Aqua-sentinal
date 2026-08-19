"""
Anomaly Detection background worker.
Consumes ais.features → rules + statistical detection → DB + anomaly.events stream.
Also runs periodic AIS-gap check against vessels table.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

sys.path.insert(0, "/app")

from shared.db import get_pool
from shared.redis_client import get_redis, ensure_consumer_group, consume_stream, publish_to_stream
from app.rules import apply_rules, check_ais_gap, AIS_GAP_THRESHOLD_MIN
from app.statistical import update_vessel_stats, compute_z_score_anomalies, merge_anomaly_events

log = logging.getLogger(__name__)

CONSUMER_GROUP = "anomaly-detection"
CONSUMER_NAME = "anomaly-worker"
GAP_CHECK_INTERVAL_S = 120

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "features_consumed": 0,
    "anomalies_emitted": 0,
    "last_gap_check_at": None,
}


async def run_anomaly_worker() -> None:
    pool = await get_pool()
    redis = await get_redis()
    await ensure_consumer_group(redis, "ais.features", CONSUMER_GROUP)
    log.info("Anomaly detection worker started.")
    last_gap_check = 0.0

    while True:
        STATE["heartbeat"] = time.time()

        messages = await consume_stream(
            redis, "ais.features", CONSUMER_GROUP, CONSUMER_NAME,
            count=50, block_ms=2000,
        )
        for msg in messages:
            await _process_features(msg["data"], pool, redis)
            STATE["features_consumed"] += 1

        now = time.time()
        if now - last_gap_check >= GAP_CHECK_INTERVAL_S:
            last_gap_check = now
            STATE["last_gap_check_at"] = datetime.now(timezone.utc).isoformat()
            await _run_gap_check(pool, redis)


async def _process_features(features: Dict[str, Any], pool, redis) -> None:
    try:
        mmsi_raw = features.get("mmsi")
        if mmsi_raw is None:
            return
        mmsi = str(int(float(mmsi_raw)))

        # Redis Streams serialise fields as strings.  Convert the analysis
        # vector before comparing thresholds or passing it to ML models.
        for key in (
            "avg_speed", "speed_variance", "max_speed", "course_variance",
            "heading_change_rate", "loitering_score", "distance_traveled_km",
            "max_speed_delta_kn", "max_speed_drop_kn",
            "mean_cog_heading_divergence_deg", "max_cog_heading_divergence_deg",
            "max_rate_of_turn_deg_min", "draught_m", "draught_change_m",
        ):
            if features.get(key) not in (None, ""):
                try:
                    features[key] = float(features[key])
                except (TypeError, ValueError):
                    features[key] = None
        for key in ("ping_count", "repeated_cog_heading_divergences", "turn_reversal_count"):
            if features.get(key) not in (None, ""):
                try:
                    features[key] = int(float(features[key]))
                except (TypeError, ValueError):
                    features[key] = 0

        vessel_row = await pool.fetchrow(
            """SELECT id, vessel_type, destination, last_lat, last_lon, last_seen,
                      last_course, last_heading, last_draught
               FROM vessels WHERE mmsi=$1""",
            mmsi,
        )
        vessel_meta = dict(vessel_row) if vessel_row else {
            "vessel_type": "unknown", "destination": None, "last_lat": None, "last_lon": None,
            "last_course": None, "last_heading": None, "last_draught": None,
        }
        vessel_type = (vessel_meta.get("vessel_type") or "unknown").lower()

        prev_row = await pool.fetchrow(
            "SELECT avg_speed FROM vessel_features WHERE mmsi=$1 ORDER BY window_end DESC LIMIT 1 OFFSET 1",
            mmsi,
        )
        previous_features = dict(prev_row) if prev_row else None

        port_nearby = False
        if vessel_meta.get("last_lat") and vessel_meta.get("last_lon"):
            try:
                from app.port_cache import is_near_port
                port_nearby = is_near_port(float(vessel_meta["last_lat"]), float(vessel_meta["last_lon"])) is not None
            except Exception:
                pass

        rule_events = apply_rules(features, vessel_meta, previous_features, port_nearby)
        await update_vessel_stats(redis, mmsi, features)
        stat_events = await compute_z_score_anomalies(redis, features, vessel_type)
        all_events = merge_anomaly_events(rule_events, stat_events)

        for event in all_events:
            await _save_and_publish_event(event, pool, redis)

    except Exception as exc:
        log.exception("Error processing features for MMSI %s: %s", features.get("mmsi"), exc)


async def _save_and_publish_event(event: Dict[str, Any], pool, redis) -> None:
    try:
        mmsi = str(event["mmsi"])
        window_start = event.get("window_start")
        if isinstance(window_start, str):
            try:
                window_start = datetime.fromisoformat(window_start.replace("Z", "+00:00"))
            except ValueError:
                window_start = datetime.now(timezone.utc)

        evidence = event.get("evidence", {})
        if not isinstance(evidence, str):
            evidence = json.dumps(evidence)

        vessel = await pool.fetchrow(
            "SELECT id, last_lat, last_lon FROM vessels WHERE mmsi=$1", mmsi
        )
        confidence = event.get("confidence_score")
        if confidence is None:
            confidence = {"LOW": 0.45, "MEDIUM": 0.65, "HIGH": 0.82, "CRITICAL": 0.94}.get(
                event.get("severity", "LOW"), 0.45
            )
        confidence = max(0.0, min(1.0, float(confidence)))

        await pool.execute(
            """
            INSERT INTO anomaly_events
                (vessel_id, mmsi, window_start, anomaly_type, severity, evidence, source,
                 latitude, longitude, confidence_score)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)
            ON CONFLICT DO NOTHING
            """,
            vessel["id"] if vessel else None,
            mmsi, window_start, event["anomaly_type"], event["severity"], evidence,
            event.get("source", "rules"),
            event.get("latitude", vessel["last_lat"] if vessel else None),
            event.get("longitude", vessel["last_lon"] if vessel else None),
            confidence,
        )
        await publish_to_stream(redis, "anomaly.events", {
            "mmsi": mmsi, "anomaly_type": event["anomaly_type"],
            "severity": event["severity"], "window_start": str(window_start),
            "source": event.get("source", "rules"),
            "evidence": json.dumps(event.get("evidence", {})),
            "latitude": event.get("latitude", vessel["last_lat"] if vessel else ""),
            "longitude": event.get("longitude", vessel["last_lon"] if vessel else ""),
            "confidence_score": confidence,
        })
        STATE["anomalies_emitted"] += 1
        log.info("Anomaly: MMSI=%s type=%s severity=%s", mmsi, event["anomaly_type"], event["severity"])
    except Exception as e:
        log.error("Failed to save anomaly event: %s | event=%r", e, event)


async def _run_gap_check(pool, redis) -> None:
    try:
        rows = await pool.fetch(
            """
            SELECT mmsi, vessel_type, last_seen, last_lat, last_lon
            FROM vessels
            WHERE last_seen >= NOW() - INTERVAL '24 hours'
              AND last_seen < NOW() - ($1 || ' minutes')::INTERVAL
            """,
            str(AIS_GAP_THRESHOLD_MIN),
        )
        for row in rows:
            gap_event = check_ais_gap(row["mmsi"], row["last_seen"], dict(row))
            if gap_event:
                existing = await pool.fetchrow(
                    """SELECT id FROM anomaly_events
                    WHERE mmsi=$1 AND anomaly_type='ais_gap'
                      AND window_start >= NOW() - INTERVAL '2 hours'""",
                    row["mmsi"],
                )
                if not existing:
                    await _save_and_publish_event(gap_event, pool, redis)
    except Exception as e:
        log.error("Gap check failed: %s", e)
