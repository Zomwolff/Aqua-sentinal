"""AIS Spoof Detection background worker — consumes ais.clean, scores trust per ping."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

sys.path.insert(0, "/app")

from shared.db import get_pool
from shared.redis_client import get_redis, ensure_consumer_group, consume_stream, publish_to_stream
from app.trust_scorer import (
    score_dead_reckoning,
    score_speed_jump,
    score_mmsi_collision,
    score_identity_consistency,
    score_mmsi_validity,
    compute_instant_trust_score,
    update_rolling_trust,
    SPOOFING_TRUST_THRESHOLD,
)

log = logging.getLogger(__name__)

CONSUMER_GROUP = "spoof-detection"
CONSUMER_NAME = "spoof-worker"

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "pings_processed": 0,
    "spoofing_flags_raised": 0,
}

# In-memory previous-ping cache: {mmsi: {lat, lon, ts, vessel_name, vessel_type}}
_prev_ping_cache: Dict[int, Dict[str, Any]] = {}


async def run_spoof_worker() -> None:
    pool = await get_pool()
    redis = await get_redis()
    await ensure_consumer_group(redis, "ais.clean", CONSUMER_GROUP)
    log.info("AIS Spoof Detection worker started (5-factor scoring).")

    while True:
        STATE["heartbeat"] = time.time()
        messages = await consume_stream(
            redis, "ais.clean", CONSUMER_GROUP, CONSUMER_NAME,
            count=200, block_ms=1000,
        )
        for msg in messages:
            await _process_ping(msg["data"], pool, redis)
            STATE["pings_processed"] += 1


async def _process_ping(data: Dict[str, Any], pool, redis) -> None:
    try:
        mmsi = int(data.get("mmsi", 0))
        if not mmsi:
            return

        lat_raw = data.get("lat")
        lon_raw = data.get("lon")
        if lat_raw is None or lon_raw is None:
            return

        lat, lon = float(lat_raw), float(lon_raw)
        ts_raw = data.get("timestamp")
        if isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(timezone.utc)
        elif isinstance(ts_raw, datetime):
            ts = ts_raw
        else:
            ts = datetime.now(timezone.utc)

        vessel_name    = data.get("vessel_name")
        vessel_type    = data.get("vessel_type_str")
        speed_knots    = float(data.get("speed_knots") or 0.0)
        heading_deg    = float(data.get("heading") or data.get("course") or 0.0)

        prev = _prev_ping_cache.get(mmsi)

        # ── Factor 5: MMSI validity ──────────────────────────────────────────
        mmsi_trust, mmsi_valid = score_mmsi_validity(mmsi)

        # ── Factor 2: Raw speed jump ─────────────────────────────────────────
        speed_jump_trust, teleport_detected = 1.0, False
        if prev is not None:
            speed_jump_trust, teleport_detected = score_speed_jump(
                prev["lat"], prev["lon"], prev["ts"], lat, lon, ts,
            )

        # ── Factor 1: Dead-reckoning position check ──────────────────────────
        dr_trust, dr_discrepancy_m = 1.0, 0.0
        if prev is not None and prev.get("speed_knots") is not None:
            dr_trust, dr_discrepancy_m = score_dead_reckoning(
                prev["lat"], prev["lon"],
                prev["speed_knots"], prev["heading_deg"],
                prev["ts"],
                lat, lon, ts,
            )

        # ── Factor 3: MMSI collision ─────────────────────────────────────────
        collision_trust, collision_detected = await score_mmsi_collision(pool, mmsi, lat, lon)

        # ── Factor 4: Identity consistency ───────────────────────────────────
        identity_trust, identity_changed = score_identity_consistency(
            mmsi, vessel_name, vessel_type,
            prev.get("vessel_name") if prev else None,
            prev.get("vessel_type") if prev else None,
        )

        # ── Update cache with speed & heading for next ping ──────────────────
        _prev_ping_cache[mmsi] = {
            "lat": lat, "lon": lon, "ts": ts,
            "vessel_name": vessel_name, "vessel_type": vessel_type,
            "speed_knots": speed_knots, "heading_deg": heading_deg,
        }

        # ── Combined 5-factor score ──────────────────────────────────────────
        instant_score = compute_instant_trust_score(
            dr_trust, speed_jump_trust, collision_trust, identity_trust, mmsi_trust,
        )
        rolling_score = await update_rolling_trust(redis, mmsi, instant_score)

        # Write to DB only when something notable happened
        is_suspicious = rolling_score < SPOOFING_TRUST_THRESHOLD
        has_flags = teleport_detected or identity_changed or not mmsi_valid or collision_detected

        if has_flags or is_suspicious:
            flag = "spoofing_suspected" if is_suspicious else None
            try:
                await pool.execute(
                    """INSERT INTO ais_trust_scores
                        (mmsi, timestamp, discrepancy_distance_m, instant_trust_score,
                         rolling_trust_score, speed_jump_flag, identity_change_flag,
                         mmsi_validity_flag, flag)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    str(mmsi), ts, dr_discrepancy_m if dr_discrepancy_m > 0 else None,
                    instant_score, rolling_score,
                    teleport_detected or (dr_trust < 0.5),
                    identity_changed, mmsi_valid, flag,
                )
            except Exception as e:
                log.error("DB insert ais_trust_scores MMSI %d: %s", mmsi, e)

            try:
                await publish_to_stream(redis, "ais.trust", {
                    "mmsi": mmsi, "timestamp": ts.isoformat(),
                    "instant_trust_score": instant_score,
                    "rolling_trust_score": rolling_score,
                    "speed_jump_flag": teleport_detected,
                    "dr_discrepancy_m": dr_discrepancy_m,
                    "collision_flag": collision_detected,
                    "identity_change_flag": identity_changed,
                    "mmsi_validity_flag": mmsi_valid,
                    "flag": flag or "",
                })
            except Exception as e:
                log.error("Publish ais.trust MMSI %d: %s", mmsi, e)

            if is_suspicious:
                STATE["spoofing_flags_raised"] += 1
                log.warning(
                    "SPOOFING SUSPECTED: MMSI=%d rolling=%.3f dr_discrep=%.0fm "
                    "teleport=%s collision=%s identity=%s",
                    mmsi, rolling_score, dr_discrepancy_m,
                    teleport_detected, collision_detected, identity_changed,
                )

    except Exception as exc:
        log.exception("Error processing ping MMSI %s: %s", data.get("mmsi"), exc)

