"""
Data-ingestion background worker.

Responsibilities:
1. Connect to the live AIS provider (AISStream.io)
2. For each message: validate → dedup → normalise → upsert_vessel → insert_position → publish ais.clean
3. Track ingestion stats for the /ingest/status endpoint
4. Handle static data messages (ShipStaticData) — update vessel table only, don't publish a position
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.db import get_pool, upsert_vessel, insert_position
from shared.redis_client import get_redis, publish_to_stream
from app.validator import validate_ais_record
from app.deduplicator import AISDeduplicator
from app.normalizer import normalize_timestamp
from app.live_providers.aisstream import stream_ais_records

log = logging.getLogger(__name__)

# Global state exposed to /health and /ingest/status endpoints
STATE: Dict[str, Any] = {
    "heartbeat": None,
    "messages_processed": 0,
    "messages_rejected": 0,
    "rejection_reasons": defaultdict(int),
    "last_message_at": None,
    "provider": "unknown",
    "started_at": None,
}


async def run_ingestion_worker() -> None:
    """
    Main worker loop. Runs forever; designed to be launched as an asyncio Task.
    Contains the full AISStream connection logic inline to avoid async generator
    / sub-task interactions that cause spurious cancellations in uvicorn.
    """
    provider = os.environ.get("AIS_PROVIDER", "aisstream")
    api_key = os.environ.get("AISSTREAM_API_KEY", "")

    STATE["provider"] = provider
    STATE["started_at"] = datetime.now(timezone.utc).isoformat()

    pool = await get_pool()
    redis = await get_redis()
    deduplicator = AISDeduplicator(redis)

    if provider == "none":
        log.info("AIS_PROVIDER=none. Accepting only POST /ingest/ais.")
        while True:
            STATE["heartbeat"] = time.time()
            await asyncio.sleep(10)
        return

    if provider != "aisstream":
        log.error("Unknown AIS_PROVIDER: %s", provider)
        while True:
            STATE["heartbeat"] = time.time()
            await asyncio.sleep(10)
        return

    if not api_key:
        log.error("AIS_PROVIDER=aisstream but AISSTREAM_API_KEY is not set.")
        while True:
            STATE["heartbeat"] = time.time()
            await asyncio.sleep(10)
        return

    log.info("Starting AISStream.io live ingestion ...")

    from app.live_providers.aisstream import (
        AISSSTREAM_URL, _build_subscription,
        _decode_position_report, _decode_ship_static,
    )
    import websockets
    from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

    subscription = _build_subscription(api_key)
    backoff = 5

    while True:
        try:
            log.info("Connecting to AISStream.io ...")
            async with websockets.connect(
                AISSSTREAM_URL,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=10,
                open_timeout=15,
                max_size=2**20,
            ) as ws:
                await ws.send(subscription)
                log.info(
                    "AISStream.io connected. Bbox: lat[%s-%s] lon[%s-%s]. Listening...",
                    os.environ.get("AIS_BBOX_MIN_LAT", "14.0"),
                    os.environ.get("AIS_BBOX_MAX_LAT", "25.0"),
                    os.environ.get("AIS_BBOX_MIN_LON", "68.0"),
                    os.environ.get("AIS_BBOX_MAX_LON", "77.5"),
                )
                backoff = 5

                while True:
                    STATE["heartbeat"] = time.time()
                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=90)
                    except asyncio.TimeoutError:
                        # No ships transmitting in bbox right now — connection alive
                        log.debug("AISStream.io: bbox quiet for 90s — still connected.")
                        continue
                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("MessageType", "")
                    record = None
                    if msg_type in (
                        "PositionReport",
                        "ExtendedClassBPositionReport",
                        "StandardClassBPositionReport",
                    ):
                        record = _decode_position_report(msg)
                    elif msg_type == "ShipStaticData":
                        record = _decode_ship_static(msg)

                    if record is not None:
                        await _process_record(record, pool, redis, deduplicator)

        except asyncio.CancelledError:
            log.info("Ingestion worker cancelled — shutting down.")
            return

        except (ConnectionClosedOK,):
            log.info("AISStream.io closed cleanly — reconnecting in %ds.", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

        except Exception as exc:
            log.warning("AISStream.io error: %s — reconnecting in %ds.", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


async def _process_record(
    record: Dict[str, Any],
    pool,
    redis,
    deduplicator: AISDeduplicator,
) -> None:
    """
    Full processing pipeline for a single AIS record.
    Errors are caught per-step so one bad record never kills the worker.
    """
    try:
        # ── Step 0: Normalise timestamp if not already done ────────────────────
        if not isinstance(record.get("timestamp"), datetime):
            ts = normalize_timestamp(record.get("timestamp"))
            if ts is None:
                _reject("missing_timestamp")
                return
            record["timestamp"] = ts

        # ── Step 1: Static-only messages (ShipStaticData) ──────────────────────
        is_static_only = record.get("lat") is None and record.get("lon") is None
        if is_static_only:
            # Update vessel metadata only
            try:
                await upsert_vessel(pool, record)
            except Exception as e:
                log.warning("upsert_vessel failed for MMSI %s (static): %s", record.get("mmsi"), e)
            return  # no position to insert or publish

        # ── Step 2: Validate ───────────────────────────────────────────────────
        valid, reason = validate_ais_record(record)
        if not valid:
            _reject(reason)
            return

        # ── Step 3: Deduplicate ────────────────────────────────────────────────
        mmsi = record["mmsi"]  # keep as original type; stringify in db.py
        ts_epoch = record["timestamp"].timestamp()
        vessel_name = record.get("vessel_name")

        is_dup, dup_flag = await deduplicator.is_duplicate(mmsi, ts_epoch, vessel_name)
        if is_dup:
            _reject("duplicate")
            return
        if dup_flag:
            record["anomaly_flag"] = dup_flag  # carry forward for downstream

        # ── Step 4: Upsert vessel metadata ────────────────────────────────────
        vessel_id: Optional[int] = None
        try:
            vessel_id = await upsert_vessel(pool, record)
        except Exception as e:
            log.warning("upsert_vessel failed for MMSI %s: %s", record.get("mmsi"), e)
            # Non-fatal: continue with position insert if we can get the id another way
            from shared.db import get_or_create_vessel_id
            try:
                vessel_id = await get_or_create_vessel_id(pool, record["mmsi"])
            except Exception:
                pass

        # ── Step 5: Insert position ────────────────────────────────────────────
        if vessel_id is not None:
            try:
                await insert_position(pool, vessel_id, record)
            except Exception as e:
                log.error("insert_position failed for MMSI %s: %s", record.get("mmsi"), e)
                return  # If we can't write the position, don't publish either
        else:
            log.warning("No vessel_id for MMSI %s — skipping position insert", record.get("mmsi"))
            return

        # ── Step 6: Publish to ais.clean stream ───────────────────────────────
        try:
            clean_record = {
                "mmsi": str(mmsi),
                "vessel_id": str(vessel_id) if vessel_id else "",
                "lat": record["lat"],
                "lon": record["lon"],
                "speed_knots": record.get("speed_knots"),
                "course": record.get("course"),
                "heading": record.get("heading"),
                "nav_status": record.get("nav_status"),
                "timestamp": record["timestamp"].isoformat(),
                "vessel_name": record.get("vessel_name"),
                "vessel_type_str": record.get("vessel_type_str", "unknown"),
                "quality_flag": record.get("quality_flag", "raw"),
                "raw_source": record.get("raw_source", "aisstream"),
            }
            await publish_to_stream(redis, "ais.clean", clean_record)
        except Exception as e:
            log.error("publish_to_stream ais.clean failed for MMSI %s: %s", mmsi, e)

        # ── Step 7: Update stats ───────────────────────────────────────────────
        STATE["messages_processed"] += 1
        STATE["last_message_at"] = record["timestamp"].isoformat()

    except Exception as exc:
        log.exception("Unexpected error processing record MMSI %s: %s", record.get("mmsi"), exc)
        _reject("unexpected_error")


def _reject(reason: str) -> None:
    """Increment rejection counters."""
    STATE["messages_rejected"] += 1
    STATE["rejection_reasons"][reason] += 1


async def ingest_ais_batch(
    records: list,
    pool,
    redis,
    deduplicator: AISDeduplicator,
) -> Dict[str, Any]:
    """
    Process a batch of AIS records submitted via POST /ingest/ais.
    Returns an ingestion result summary.
    """
    accepted = 0
    rejected = 0
    rejection_reasons: Dict[str, int] = defaultdict(int)

    for record in records:
        if not isinstance(record.get("timestamp"), datetime):
            ts = normalize_timestamp(record.get("timestamp"))
            if ts is None:
                rejected += 1
                rejection_reasons["missing_timestamp"] += 1
                continue
            record["timestamp"] = ts

        valid, reason = validate_ais_record(record)
        if not valid:
            rejected += 1
            rejection_reasons[reason or "unknown"] += 1
            continue

        mmsi = record["mmsi"]
        is_dup, _ = await deduplicator.is_duplicate(mmsi, record["timestamp"].timestamp())
        if is_dup:
            rejected += 1
            rejection_reasons["duplicate"] += 1
            continue

        try:
            vessel_id = await upsert_vessel(pool, record)
            await insert_position(pool, vessel_id, record)
            await publish_to_stream(redis, "ais.clean", {
                "mmsi": str(mmsi),
                "vessel_id": str(vessel_id),
                "lat": record["lat"],
                "lon": record["lon"],
                "speed_knots": record.get("speed_knots"),
                "course": record.get("course"),
                "heading": record.get("heading"),
                "nav_status": record.get("nav_status"),
                "timestamp": record["timestamp"].isoformat(),
                "vessel_name": record.get("vessel_name"),
                "vessel_type_str": record.get("vessel_type_str", "unknown"),
                "quality_flag": record.get("quality_flag", "raw"),
                "raw_source": record.get("raw_source", "http_post"),
            })
            accepted += 1
        except Exception as e:
            log.error("Batch ingest DB/publish error for MMSI %s: %s", mmsi, e)
            rejected += 1
            rejection_reasons["db_error"] += 1

    return {"accepted": accepted, "rejected": rejected, "rejection_reasons": dict(rejection_reasons)}
