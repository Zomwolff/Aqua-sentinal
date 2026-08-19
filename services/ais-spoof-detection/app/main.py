"""AIS Spoof Detection Service — FastAPI entry point."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, "/app")

from shared.db import get_pool, close_pool
from shared.redis_client import get_redis, close_redis
from app.worker import run_spoof_worker, STATE
from app.trust_scorer import get_rolling_trust_score, SPOOFING_TRUST_THRESHOLD

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
SERVICE_NAME = "ais-spoof-detection"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    pool = await get_pool()
    redis = await get_redis()
    app.state.pool = pool
    app.state.redis = redis
    task = asyncio.create_task(run_spoof_worker())
    app.state.worker_task = task
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await close_pool()
    await close_redis()


app = FastAPI(title="Aqua-Sentinel: AIS Spoof Detection", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    hb = STATE["heartbeat"]
    return {
        "status": "ok", "service": SERVICE_NAME,
        "worker_alive": hb is not None and (time.time() - hb) < 90,
        "pings_processed": STATE["pings_processed"],
        "spoofing_flags_raised": STATE["spoofing_flags_raised"],
    }


@app.get("/vessels/{mmsi}/trust-score")
async def get_trust_score(mmsi: int):
    redis = app.state.redis
    pool = app.state.pool
    rolling_score = await get_rolling_trust_score(redis, mmsi)
    rows = await pool.fetch(
        "SELECT timestamp, instant_trust_score, rolling_trust_score, "
        "speed_jump_flag, identity_change_flag, flag "
        "FROM ais_trust_scores WHERE mmsi=$1 ORDER BY timestamp DESC LIMIT 50",
        mmsi,
    )
    return {
        "mmsi": mmsi,
        "current_rolling_trust_score": rolling_score,
        "flag": "spoofing_suspected" if rolling_score < SPOOFING_TRUST_THRESHOLD else None,
        "history": [{
            "timestamp": r["timestamp"].isoformat(),
            "instant": r["instant_trust_score"],
            "rolling": r["rolling_trust_score"],
            "speed_jump": r["speed_jump_flag"],
            "identity_change": r["identity_change_flag"],
            "flag": r["flag"],
        } for r in rows],
    }


@app.get("/spoofing-suspects")
async def list_spoofing_suspects(threshold: float = Query(SPOOFING_TRUST_THRESHOLD, ge=0.0, le=1.0)):
    pool = app.state.pool
    rows = await pool.fetch(
        """SELECT DISTINCT ON (mmsi) mmsi, rolling_trust_score, flag, timestamp
           FROM ais_trust_scores WHERE rolling_trust_score < $1
           ORDER BY mmsi, timestamp DESC""",
        threshold,
    )
    return [{
        "mmsi": r["mmsi"], "rolling_trust_score": r["rolling_trust_score"],
        "flag": r["flag"], "last_seen": r["timestamp"].isoformat(),
    } for r in rows]
