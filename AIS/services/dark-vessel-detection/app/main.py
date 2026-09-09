"""
Dark Vessel Detection Service — FastAPI entry point.

Provides:
  GET /health               — liveness + worker heartbeat
  GET /dark-vessels         — recent dark vessel events (for API gateway)
  POST /dark-vessels/scan   — manually trigger an immediate AIS-gap scan
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

sys.path.insert(0, "/app")
from shared.db import get_pool, close_pool
from shared.redis_client import get_redis, close_redis
from app.detector import run_dark_vessel_detector, run_ais_gap_scan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("dark-vessel-detection")

SERVICE_NAME = "dark-vessel-detection"

_state: Dict[str, Any] = {
    "heartbeat": None,
    "events_emitted": 0,
    "started_at": None,
}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    log.info("Starting %s...", SERVICE_NAME)
    pool  = await get_pool()
    redis = await get_redis()
    app.state.pool  = pool
    app.state.redis = redis
    _state["started_at"] = datetime.now(timezone.utc).isoformat()

    task = asyncio.create_task(_worker_wrapper())
    app.state.worker_task = task
    log.info("%s startup complete.", SERVICE_NAME)

    yield

    log.info("Shutting down %s...", SERVICE_NAME)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await close_pool()
    await close_redis()


async def _worker_wrapper() -> None:
    """Wraps the detector loop so we can track heartbeat + events."""
    while True:
        try:
            _state["heartbeat"] = time.time()
            await run_dark_vessel_detector(on_heartbeat=_record_heartbeat)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Dark vessel detector crashed, restarting in 10s: %s", exc)
            await asyncio.sleep(10)


def _record_heartbeat() -> None:
    _state["heartbeat"] = time.time()


app = FastAPI(title=SERVICE_NAME, version="1.0.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    heartbeat = _state.get("heartbeat")
    worker_alive = heartbeat is not None and (time.time() - heartbeat) < 30
    return {
        "status": "ok" if worker_alive else "degraded",
        "service": SERVICE_NAME,
        "worker_alive": worker_alive,
        "started_at": _state.get("started_at"),
    }


@app.get("/dark-vessels")
async def get_dark_vessels(
    limit: int = Query(50, ge=1, le=500),
    hours: int = Query(24, ge=1, le=168),
):
    """Return recent dark vessel events ordered by detection time descending."""
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, detected_at, latitude, longitude,
                  image_source, sensor, confidence,
                  length_est_m, matched_mmsi
           FROM dark_vessel_events
           WHERE detected_at >= NOW() - ($1 || ' hours')::INTERVAL
           ORDER BY detected_at DESC
           LIMIT $2""",
        str(hours), limit,
    )
    return JSONResponse([
        {
            "id": r["id"],
            "detected_at": r["detected_at"].isoformat() if r["detected_at"] else None,
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "image_source": r["image_source"],
            "sensor": r["sensor"],
            "confidence": r["confidence"],
            "length_est_m": r["length_est_m"],
            "matched_mmsi": r["matched_mmsi"],
        }
        for r in rows
    ])


@app.post("/dark-vessels/scan")
async def trigger_scan():
    """Manually trigger an immediate AIS-gap scan (for testing/admin)."""
    pool  = await get_pool()
    redis = await get_redis()
    count = await run_ais_gap_scan(pool, redis)
    return {"status": "ok", "new_events": count}

