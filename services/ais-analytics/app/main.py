"""
AIS Analytics Service — FastAPI application entry point.

Provides:
  GET /health
  GET /vessels                          — list all active vessels
  GET /vessels/{mmsi}/features          — latest feature window for a vessel
  GET /vessels/{mmsi}/proximity         — current proximity events for a vessel
  POST /features/recompute              — manual window flush trigger
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, "/app")

from shared.db import get_pool, close_pool, get_all_active_vessels
from shared.redis_client import get_redis, close_redis
from app.worker import run_analytics_worker, STATE
from app.reference_loader_cache import init_port_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ais-analytics")
SERVICE_NAME = "ais-analytics"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    pool = await get_pool()
    redis = await get_redis()
    app.state.pool = pool
    app.state.redis = redis
    await init_port_cache()
    task = asyncio.create_task(run_analytics_worker())
    app.state.worker_task = task
    log.info("%s started.", SERVICE_NAME)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await close_pool()
    await close_redis()


app = FastAPI(
    title="Aqua-Sentinel: AIS Analytics",
    description="Per-vessel behavioral feature extraction from the AIS stream.",
    version="1.0.0",
    lifespan=_lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    hb = STATE["heartbeat"]
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "worker_alive": hb is not None and (time.time() - hb) < 90,
        "messages_consumed": STATE["messages_consumed"],
        "windows_flushed": STATE["windows_flushed"],
        "last_flush_at": STATE["last_flush_at"],
    }


@app.get("/vessels")
async def list_vessels(
    active_minutes: int = Query(30, ge=1, le=1440, description="Only vessels seen within last N minutes"),
    request=None,
):
    """List all vessels active in the last N minutes with latest risk/position."""
    from fastapi import Request
    # Get pool from app state
    import asyncio
    from fastapi import Request as Req
    pool = app.state.pool
    rows = await pool.fetch(
        """
        SELECT mmsi, vessel_name, vessel_type, last_lat, last_lon, last_seen
        FROM vessels
        WHERE last_seen >= NOW() - ($1 || ' minutes')::INTERVAL
          AND last_lat IS NOT NULL
        ORDER BY last_seen DESC
        LIMIT 500
        """,
        str(active_minutes),
    )
    return [{"mmsi": r["mmsi"], "vessel_name": r["vessel_name"],
             "vessel_type": r["vessel_type"], "lat": r["last_lat"],
             "lon": r["last_lon"], "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None}
            for r in rows]


@app.get("/vessels/{mmsi}/features")
async def get_vessel_features(mmsi: int):
    """Return the most recent behavioral feature window for a vessel."""
    pool = app.state.pool
    row = await pool.fetchrow(
        """
        SELECT * FROM vessel_features WHERE mmsi=$1
        ORDER BY window_end DESC LIMIT 1
        """,
        mmsi,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"No features found for MMSI {mmsi}")
    result = dict(row)
    # Deserialise JSONB field
    if isinstance(result.get("proximity_events"), str):
        try:
            result["proximity_events"] = json.loads(result["proximity_events"])
        except json.JSONDecodeError:
            result["proximity_events"] = []
    return result


@app.get("/vessels/{mmsi}/proximity")
async def get_vessel_proximity(mmsi: int):
    """Return the most recent proximity events for a vessel (who is nearby)."""
    pool = app.state.pool
    row = await pool.fetchrow(
        "SELECT proximity_events, window_end FROM vessel_features "
        "WHERE mmsi=$1 ORDER BY window_end DESC LIMIT 1",
        mmsi,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"No proximity data for MMSI {mmsi}")
    events = row["proximity_events"]
    if isinstance(events, str):
        try:
            events = json.loads(events)
        except json.JSONDecodeError:
            events = []
    return {"mmsi": mmsi, "window_end": row["window_end"].isoformat(), "proximity_events": events or []}


class RecomputeRequest(BaseModel):
    mmsi: Optional[int] = None  # if None, recompute all active vessels


@app.post("/features/recompute")
async def recompute_features(req: RecomputeRequest):
    """
    Manually trigger a feature window flush.
    If mmsi is provided, flushes only that vessel. Otherwise flushes all active vessels.
    Returns immediately with a job acknowledgement (flush runs in background).
    """
    from app.worker import _flush_window, _last_flush
    from app.window_buffer import WindowBuffer

    redis = app.state.redis
    pool = app.state.pool
    buffer = WindowBuffer(redis)
    now_dt = datetime.now(timezone.utc)

    try:
        active = await get_all_active_vessels(pool, active_within_minutes=60)
    except Exception:
        active = []

    if req.mmsi:
        targets = [req.mmsi]
    else:
        targets = list(_last_flush.keys())
        if not targets:
            return {"status": "no_active_vessels"}

    # Trigger flushes as background tasks
    for m in targets:
        asyncio.create_task(_flush_window(m, buffer, pool, redis, active, now_dt))

    return {"status": "flushing", "vessel_count": len(targets)}
