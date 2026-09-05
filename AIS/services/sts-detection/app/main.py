"""STS Detection Service — FastAPI entry point."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, "/app")

from shared.db import get_pool, close_pool
from shared.redis_client import get_redis, close_redis
from app.worker import run_sts_worker, STATE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
SERVICE_NAME = "sts-detection"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    pool = await get_pool()
    redis = await get_redis()
    app.state.pool = pool
    app.state.redis = redis
    task = asyncio.create_task(run_sts_worker())
    app.state.worker_task = task
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await close_pool()
    await close_redis()


app = FastAPI(title="Aqua-Sentinel: STS Detection", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    hb = STATE["heartbeat"]
    return {
        "status": "ok", "service": SERVICE_NAME,
        "worker_alive": hb is not None and (time.time() - hb) < 90,
        "features_consumed": STATE["features_consumed"],
        "sts_events_emitted": STATE["sts_events_emitted"],
    }


@app.get("/sts-events")
async def list_sts_events(
    mmsi: Optional[int] = Query(None),
    since: Optional[str] = Query(None),
    limit: int = Query(50, le=500),
):
    """Return completed STS events from the database."""
    pool = app.state.pool
    conditions: List[str] = []
    params: List[Any] = []
    idx = 1

    if mmsi:
        conditions.append(f"(vessel_a=${idx} OR vessel_b=${idx})")
        params.append(mmsi)
        idx += 1
    if since:
        from datetime import datetime
        dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        conditions.append(f"start_time >= ${idx}")
        params.append(dt)
        idx += 1

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)
    rows = await pool.fetch(
        f"SELECT id, vessel_a, vessel_b, start_time, end_time, duration_minutes, "
        f"avg_distance_m, min_distance_m, avg_combined_speed_knots, confidence "
        f"FROM sts_events {where} ORDER BY start_time DESC LIMIT ${idx}",
        *params,
    )
    result = []
    for r in rows:
        row = dict(r)
        for k in ("start_time", "end_time"):
            if row.get(k):
                row[k] = row[k].isoformat()
        result.append(row)
    return result


@app.get("/sts-events/active")
async def list_active_sts():
    """Return currently active STS states from Redis."""
    redis = app.state.redis
    keys = await redis.keys("sts:active:*")
    active = []
    for key in keys:
        raw = await redis.hgetall(key)
        if raw:
            parts = key.split(":")
            if len(parts) >= 4:
                active.append({
                    "vessel_a": parts[2], "vessel_b": parts[3],
                    "start_time": raw.get("start_time"),
                    "duration_s": raw.get("total_duration_s"),
                    "emitted": raw.get("emitted"),
                    "sample_count": raw.get("sample_count"),
                })
    return active
