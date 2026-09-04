"""
STS encounter state machine backed by Redis hashes.

State per pair: sts:active:{min(a,b)}:{max(a,b)}
Lifecycle: OPEN → ACTIVE (conditions met) → EMITTED (min duration reached) → CLOSED (conditions broken or stale)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

STS_DISTANCE_THRESHOLD_M = float(os.environ.get("STS_DISTANCE_THRESHOLD_M", 500))
STS_SPEED_THRESHOLD_KN   = float(os.environ.get("STS_SPEED_THRESHOLD_KN", 2.0))
STS_MIN_DURATION_MIN     = float(os.environ.get("STS_MIN_DURATION_MIN", 30))
STALE_TIMEOUT_S          = float(os.environ.get("WINDOW_MINUTES", 15)) * 60 * 2


def _pair_key(a: int, b: int) -> str:
    lo, hi = min(a, b), max(a, b)
    return f"sts:active:{lo}:{hi}"


async def update_pair(
    redis,
    mmsi_a: int, mmsi_b: int,
    distance_m: float,
    speed_a: float, speed_b: float,
    lat: float, lon: float,
    ts: datetime,
) -> Optional[Dict[str, Any]]:
    """
    Update STS state machine for a vessel pair.
    Returns an STS event dict if the pair has just crossed the minimum duration
    threshold (and hasn't been emitted yet), else None.
    """
    key = _pair_key(mmsi_a, mmsi_b)
    now_epoch = ts.timestamp()
    avg_speed = (speed_a + speed_b) / 2.0

    meets = distance_m <= STS_DISTANCE_THRESHOLD_M and avg_speed <= STS_SPEED_THRESHOLD_KN
    raw = await redis.hgetall(key)

    if not meets:
        if raw:
            start_epoch = float(raw.get("start_time", now_epoch))
            total_s = now_epoch - start_epoch
            already_emitted = raw.get("emitted", "false") == "true"
            await redis.delete(key)
            if not already_emitted and total_s >= STS_MIN_DURATION_MIN * 60:
                return _build_event(mmsi_a, mmsi_b, raw, total_s)
        return None

    if not raw:
        # Open new state
        await redis.hset(key, mapping={
            "start_time": str(now_epoch), "last_seen": str(now_epoch),
            "sample_count": "1",
            "sum_distance_m": str(distance_m), "min_distance_m": str(distance_m),
            "sum_speed_a": str(speed_a), "sum_speed_b": str(speed_b),
            "sum_lat": str(lat), "sum_lon": str(lon),
            "emitted": "false",
        })
        await redis.expire(key, int(STALE_TIMEOUT_S * 3))
        return None

    # Update
    start_epoch = float(raw.get("start_time", now_epoch))
    total_s = now_epoch - start_epoch
    n = int(float(raw.get("sample_count", 0))) + 1
    sum_dist = float(raw.get("sum_distance_m", 0)) + distance_m
    min_dist = min(float(raw.get("min_distance_m", distance_m)), distance_m)
    sum_sa = float(raw.get("sum_speed_a", 0)) + speed_a
    sum_sb = float(raw.get("sum_speed_b", 0)) + speed_b
    sum_lat = float(raw.get("sum_lat", 0)) + lat
    sum_lon = float(raw.get("sum_lon", 0)) + lon
    already_emitted = raw.get("emitted", "false") == "true"

    updates = {
        "last_seen": str(now_epoch), "sample_count": str(n),
        "sum_distance_m": str(sum_dist), "min_distance_m": str(min_dist),
        "sum_speed_a": str(sum_sa), "sum_speed_b": str(sum_sb),
        "sum_lat": str(sum_lat), "sum_lon": str(sum_lon),
    }

    emit_event = None
    if not already_emitted and total_s >= STS_MIN_DURATION_MIN * 60:
        updates["emitted"] = "true"
        state_snapshot = dict(raw)
        state_snapshot.update(updates)
        emit_event = _build_event(mmsi_a, mmsi_b, state_snapshot, total_s)

    await redis.hset(key, mapping=updates)
    await redis.expire(key, int(STALE_TIMEOUT_S * 3))
    return emit_event


def _build_event(mmsi_a: int, mmsi_b: int, state: Dict[str, Any], total_s: float) -> Dict[str, Any]:
    n = max(1, int(float(state.get("sample_count", 1))))
    avg_dist = float(state.get("sum_distance_m", 0)) / n
    min_dist = float(state.get("min_distance_m", avg_dist))
    avg_speed = (float(state.get("sum_speed_a", 0)) + float(state.get("sum_speed_b", 0))) / (2 * n)
    c_lat = float(state.get("sum_lat", 0)) / n
    c_lon = float(state.get("sum_lon", 0)) / n
    start_epoch = float(state.get("start_time", 0))

    dur_score = min(1.0, total_s / (STS_MIN_DURATION_MIN * 60 * 2))
    dist_score = max(0.0, 1.0 - avg_dist / STS_DISTANCE_THRESHOLD_M)
    confidence = round(dur_score * 0.6 + dist_score * 0.4, 3)

    a, b = min(mmsi_a, mmsi_b), max(mmsi_a, mmsi_b)
    return {
        "vessel_a": a, "vessel_b": b,
        "start_time": datetime.fromtimestamp(start_epoch, tz=timezone.utc).isoformat(),
        "end_time": datetime.now(timezone.utc).isoformat(),
        "duration_minutes": round(total_s / 60.0, 1),
        "avg_distance_m": round(avg_dist, 1),
        "min_distance_m": round(min_dist, 1),
        "avg_combined_speed_knots": round(avg_speed, 2),
        "confidence": confidence,
        "centroid_lat": round(c_lat, 6),
        "centroid_lon": round(c_lon, 6),
    }


async def close_stale_pairs(redis, pool) -> int:
    """Scan for stale STS state entries and close them, emitting events if warranted."""
    keys = await redis.keys("sts:active:*")
    count = 0
    now_epoch = datetime.now(timezone.utc).timestamp()

    for key in keys:
        raw = await redis.hgetall(key)
        if not raw:
            continue
        last_seen = float(raw.get("last_seen", 0))
        if now_epoch - last_seen <= STALE_TIMEOUT_S:
            continue

        total_s = float(raw.get("total_duration_s", now_epoch - float(raw.get("start_time", now_epoch))))
        already_emitted = raw.get("emitted", "false") == "true"

        if not already_emitted and total_s >= STS_MIN_DURATION_MIN * 60:
            parts = key.split(":")
            if len(parts) >= 4:
                try:
                    a, b = int(parts[2]), int(parts[3])
                    event = _build_event(a, b, raw, total_s)
                    from app.worker import _save_sts_event
                    await _save_sts_event(event, pool)
                except Exception as e:
                    log.warning("Could not emit stale STS for %s: %s", key, e)

        await redis.delete(key)
        count += 1

    if count:
        log.info("Closed %d stale STS states", count)
    return count
