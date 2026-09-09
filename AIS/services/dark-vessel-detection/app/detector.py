"""
Dark Vessel Detector — Production-grade implementation.

Two detection mechanisms:

1. AIS-GAP DETECTION (primary, runs every DARK_VESSEL_SCAN_INTERVAL_S seconds):
   Scans the vessels table for ships that:
   - Were active recently (last_seen within the lookback window)
   - Have gone silent longer than DARK_VESSEL_GAP_MINUTES
   - Are NOT in port (spatial check against reference_layers)
   Each candidate is scored by: gap_duration, vessel_type, EEZ position, STS proximity.

2. SAR CORRELATION (secondary, consumes sar.clean stream):
   When Module B publishes a SAR detection to sar.clean, this service performs
   a spatial-temporal join against vessel_positions:
   - Looks for any AIS ping within DARK_VESSEL_MAX_MATCH_DISTANCE_M metres
     AND DARK_VESSEL_MAX_MATCH_TIME_S seconds of the SAR detection time.
   - If no AIS match found -> the SAR object is a dark vessel.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/app")
from shared.db import get_pool
from shared.redis_client import get_redis, ensure_consumer_group, consume_stream, publish_to_stream

log = logging.getLogger(__name__)

# Configuration
GAP_MINUTES          = float(os.environ.get("DARK_VESSEL_GAP_MINUTES", 90))
SCAN_INTERVAL_S      = float(os.environ.get("DARK_VESSEL_SCAN_INTERVAL_S", 300))
MAX_MATCH_DISTANCE_M = float(os.environ.get("DARK_VESSEL_MAX_MATCH_DISTANCE_M", 5000))
MAX_MATCH_TIME_S     = float(os.environ.get("DARK_VESSEL_MAX_MATCH_TIME_S", 1800))

HIGH_RISK_VESSEL_TYPES = {
    "tanker", "chemical tanker", "oil tanker", "lpg tanker", "lng tanker",
    "cargo", "bulk carrier", "general cargo",
}

SAR_CONSUMER_GROUP = "dark-vessel-detection"
SAR_CONSUMER_NAME  = "dv-sar-worker"


def _compute_ais_gap_confidence(
    gap_minutes: float,
    vessel_type: Optional[str],
    in_eez: bool,
    has_recent_sts: bool,
) -> float:
    gap_score  = min(1.0, (gap_minutes - GAP_MINUTES) / (GAP_MINUTES * 2) + 0.4)
    type_bonus = 0.15 if (vessel_type or "").lower() in HIGH_RISK_VESSEL_TYPES else 0.0
    eez_bonus  = 0.10 if in_eez else 0.0
    sts_bonus  = 0.20 if has_recent_sts else 0.0
    raw = gap_score + type_bonus + eez_bonus + sts_bonus
    return round(min(0.97, max(0.30, raw)), 3)


def _compute_sar_confidence(sar_payload_confidence: float) -> float:
    return round(min(0.95, max(0.40, sar_payload_confidence * 0.9)), 3)


async def _is_vessel_in_port(pool, lat: float, lon: float) -> bool:
    try:
        row = await pool.fetchrow(
            """SELECT id FROM reference_layers
               WHERE layer_type IN ('port', 'anchorage')
                 AND ST_DWithin(
                     geom::geography,
                     ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                     2000
                 )
               LIMIT 1""",
            lon, lat,
        )
        return row is not None
    except Exception:
        return False


async def _is_in_eez(pool, lat: float, lon: float) -> bool:
    try:
        row = await pool.fetchrow(
            """SELECT id FROM reference_layers
               WHERE layer_type = 'eez'
                 AND ST_Contains(geom, ST_SetSRID(ST_MakePoint($1, $2), 4326))
               LIMIT 1""",
            lon, lat,
        )
        return row is not None
    except Exception:
        return False


async def _has_recent_sts(pool, mmsi: str) -> bool:
    try:
        row = await pool.fetchrow(
            """SELECT id FROM sts_events
               WHERE (vessel_a_mmsi=$1 OR vessel_b_mmsi=$1)
                 AND start_time >= NOW() - INTERVAL '48 hours'
               LIMIT 1""",
            mmsi,
        )
        return row is not None
    except Exception:
        return False


async def _already_flagged_recently(pool, mmsi: str) -> bool:
    try:
        row = await pool.fetchrow(
            """SELECT id FROM dark_vessel_events
               WHERE matched_mmsi=$1
                 AND detected_at >= NOW() - INTERVAL '2 hours'
               LIMIT 1""",
            mmsi,
        )
        return row is not None
    except Exception:
        return False


async def _save_dark_vessel_event(
    pool, redis,
    lat: float, lon: float,
    matched_mmsi: Optional[str],
    image_source: str,
    sensor: str,
    confidence: float,
    length_est_m: Optional[float] = None,
) -> None:
    try:
        vessel_id = None
        if matched_mmsi:
            v = await pool.fetchrow("SELECT id FROM vessels WHERE mmsi=$1", matched_mmsi)
            vessel_id = v["id"] if v else None

        await pool.execute(
            """INSERT INTO dark_vessel_events
               (detected_at, latitude, longitude,
                geom, image_source, sensor, confidence,
                length_est_m, matched_mmsi, matched_vessel_id)
               VALUES (NOW(), $1, $2,
                       ST_SetSRID(ST_MakePoint($2, $1), 4326),
                       $3, $4, $5, $6, $7, $8)""",
            lat, lon, image_source, sensor, confidence,
            length_est_m, matched_mmsi, vessel_id,
        )
        await publish_to_stream(redis, "dark.vessel.events", {
            "matched_mmsi":  matched_mmsi or "",
            "latitude":      str(lat),
            "longitude":     str(lon),
            "confidence":    str(confidence),
            "sensor":        sensor,
            "image_source":  image_source,
            "detected_at":   datetime.now(timezone.utc).isoformat(),
        })
        log.warning(
            "DARK VESSEL: MMSI=%s lat=%.4f lon=%.4f conf=%.2f sensor=%s",
            matched_mmsi or "UNKNOWN", lat, lon, confidence, sensor,
        )
    except Exception as e:
        log.error("Failed to save dark vessel event: %s", e)


async def run_ais_gap_scan(pool, redis) -> int:
    """Periodic AIS-gap scan. Returns count of new dark vessel events emitted."""
    try:
        cutoff_gap = datetime.now(timezone.utc) - timedelta(minutes=GAP_MINUTES)
        lookback   = datetime.now(timezone.utc) - timedelta(hours=48)

        rows = await pool.fetch(
            """SELECT mmsi, vessel_type, last_lat, last_lon, last_seen
               FROM vessels
               WHERE last_seen >= $1
                 AND last_seen <  $2
                 AND last_lat IS NOT NULL
                 AND last_lon IS NOT NULL""",
            lookback, cutoff_gap,
        )

        count = 0
        for row in rows:
            mmsi      = str(row["mmsi"])
            lat       = float(row["last_lat"])
            lon       = float(row["last_lon"])
            last_seen = row["last_seen"]
            vtype     = (row["vessel_type"] or "unknown").lower()

            ls = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
            gap_minutes = (datetime.now(timezone.utc) - ls).total_seconds() / 60.0

            if await _is_vessel_in_port(pool, lat, lon):
                continue
            if await _already_flagged_recently(pool, mmsi):
                continue

            in_eez  = await _is_in_eez(pool, lat, lon)
            has_sts = await _has_recent_sts(pool, mmsi)
            conf    = _compute_ais_gap_confidence(gap_minutes, vtype, in_eez, has_sts)

            await _save_dark_vessel_event(
                pool, redis,
                lat=lat, lon=lon,
                matched_mmsi=mmsi,
                image_source="ais_gap_analysis",
                sensor="ais",
                confidence=conf,
            )
            count += 1

        if count:
            log.info("AIS-gap scan: %d new dark vessel events.", count)
        return count

    except Exception as e:
        log.error("AIS-gap scan failed: %s", e)
        return 0


async def _correlate_sar_detection(detection: Dict[str, Any], pool, redis) -> None:
    """Spatial-temporal join between a SAR object and AIS vessel_positions."""
    try:
        lat_raw  = detection.get("latitude") or detection.get("lat")
        lon_raw  = detection.get("longitude") or detection.get("lon")
        ts_raw   = detection.get("timestamp") or detection.get("detected_at")
        sar_conf = float(detection.get("confidence", 0.7))
        sensor   = detection.get("sensor", "SAR")
        source   = detection.get("source", "sar_module")
        length_est_m = detection.get("length_est_m")

        if lat_raw is None or lon_raw is None:
            return

        lat = float(lat_raw)
        lon = float(lon_raw)

        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        elif isinstance(ts_raw, datetime):
            ts = ts_raw
        else:
            ts = datetime.now(timezone.utc)

        time_lo = ts - timedelta(seconds=MAX_MATCH_TIME_S)
        time_hi = ts + timedelta(seconds=MAX_MATCH_TIME_S)

        match_row = await pool.fetchrow(
            """SELECT vp.mmsi FROM vessel_positions vp
               WHERE vp.timestamp BETWEEN $1 AND $2
                 AND ST_DWithin(
                     ST_SetSRID(ST_MakePoint(vp.longitude, vp.latitude), 4326)::geography,
                     ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                     $5
                 )
               ORDER BY vp.timestamp DESC
               LIMIT 1""",
            time_lo, time_hi, lon, lat, MAX_MATCH_DISTANCE_M,
        )

        if match_row is not None:
            log.debug("SAR at (%.4f, %.4f) matched AIS MMSI %s", lat, lon, match_row["mmsi"])
            return

        conf = _compute_sar_confidence(sar_conf)
        await _save_dark_vessel_event(
            pool, redis,
            lat=lat, lon=lon,
            matched_mmsi=None,
            image_source=source,
            sensor=sensor,
            confidence=conf,
            length_est_m=float(length_est_m) if length_est_m else None,
        )

    except Exception as e:
        log.error("SAR correlation error: %s", e)


async def run_dark_vessel_detector(on_heartbeat=None) -> None:
    """
    Main background coroutine. Runs two concurrent tasks:
      1. Periodic AIS-gap scanner (every SCAN_INTERVAL_S seconds)
      2. SAR stream consumer (event-driven, Module B integration)
    """
    pool  = await get_pool()
    redis = await get_redis()

    # sar.objects carries vectorized bright-target detections (lat/lon/length)
    # published by sar-spill-intelligence — the primary SAR-correlation input.
    # sar.clean is still consumed for backwards compatibility with any upstream
    # publisher that emits fully-formed detection records.
    await ensure_consumer_group(redis, "sar.objects", SAR_CONSUMER_GROUP)
    await ensure_consumer_group(redis, "sar.clean", SAR_CONSUMER_GROUP)
    log.info(
        "Dark Vessel Detection started. gap_threshold=%.0fmin scan_interval=%.0fs",
        GAP_MINUTES, SCAN_INTERVAL_S,
    )

    last_gap_scan = 0.0

    while True:
        if on_heartbeat is not None:
            on_heartbeat()
        now = _time.time()

        if now - last_gap_scan >= SCAN_INTERVAL_S:
            last_gap_scan = now
            await run_ais_gap_scan(pool, redis)

        try:
            messages = await consume_stream(
                redis, "sar.objects", SAR_CONSUMER_GROUP, SAR_CONSUMER_NAME,
                count=20, block_ms=200,
            )
            for msg in messages:
                payload = msg["data"]
                acq_raw = payload.get("acquisition_time")
                for obj in payload.get("objects") or []:
                    detection = dict(obj)
                    detection.setdefault("source", payload.get("scene_id", "sar_module"))
                    detection.setdefault("sensor", "SAR")
                    if acq_raw:
                        detection.setdefault("timestamp", acq_raw)
                    await _correlate_sar_detection(detection, pool, redis)
        except Exception as e:
            log.error("SAR objects stream consume error: %s", e)

        try:
            messages = await consume_stream(
                redis, "sar.clean", SAR_CONSUMER_GROUP, f"{SAR_CONSUMER_NAME}-clean",
                count=20, block_ms=100,
            )
            for msg in messages:
                await _correlate_sar_detection(msg["data"], pool, redis)
        except Exception as e:
            log.error("SAR legacy stream consume error: %s", e)

        await asyncio.sleep(1)
