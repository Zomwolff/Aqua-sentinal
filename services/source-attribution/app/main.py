"""
Source Attribution Service — FastAPI entry point.

Endpoints:
  GET /health                          — liveness + worker stats
  GET /attributions/{spill_id}         — attribution results for a spill
  GET /attributions/{spill_id}/top     — top-scoring vessel only
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

sys.path.insert(0, "/app")
from shared.db.connection import close_pool, create_pool
from shared.redis_client import close_redis, get_redis
from app.worker import STATE, run_attribution_worker

SERVICE_NAME = "source-attribution"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    pool  = await create_pool()
    redis = await get_redis()
    app.state.pool  = pool
    app.state.redis = redis
    task = asyncio.create_task(run_attribution_worker())
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


app = FastAPI(title=SERVICE_NAME, version="1.0.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/health")
async def health():
    heartbeat = STATE["heartbeat"]
    worker_alive = heartbeat is not None and (time.time() - heartbeat) < 30
    return {
        "status": "ok" if worker_alive else "degraded",
        "service": SERVICE_NAME,
        "worker_alive": worker_alive,
        **{k: STATE[k] for k in (
            "incidents_processed", "attributions_written",
            "failed", "last_candidate_id", "last_spill_id",
        )},
    }


@app.get("/attributions/{spill_id}")
async def get_attributions(spill_id: str):
    """All scored vessels for a spill, ordered by final_score desc."""
    from shared.db.connection import get_pool
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT ar.*, v.mmsi, v.name, v.vessel_type, v.flag
        FROM attribution_results ar
        JOIN vessels v ON v.id = ar.vessel_id
        WHERE ar.spill_id = $1
        ORDER BY ar.final_score DESC
        """,
        spill_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No attributions found for spill_id")
    return JSONResponse([
        {
            "vessel_id": r["vessel_id"],
            "mmsi": r["mmsi"],
            "vessel_name": r["name"],
            "vessel_type": r["vessel_type"],
            "flag": r["flag"],
            "distance_score": float(r["distance_score"] or 0),
            "trajectory_score": float(r["trajectory_score"] or 0),
            "time_score": float(r["time_score"] or 0),
            "behavior_score": float(r["behavior_score"] or 0),
            "wind_score": float(r["wind_score"] or 0),
            "final_score": float(r["final_score"]),
            "model_version": r["model_version"],
            "computed_at": r["computed_at"].isoformat() if r["computed_at"] else None,
        }
        for r in rows
    ])


@app.get("/attributions/{spill_id}/top")
async def get_top_attribution(spill_id: str):
    """Top-scoring vessel for a spill. NOTE: This is evidential, not a legal finding."""
    from shared.db.connection import get_pool
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT ar.*, v.mmsi, v.name, v.vessel_type, v.flag
        FROM attribution_results ar
        JOIN vessels v ON v.id = ar.vessel_id
        WHERE ar.spill_id = $1
        ORDER BY ar.final_score DESC
        LIMIT 1
        """,
        spill_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="No attributions found for spill_id")
    score = float(row["final_score"])
    from app.attribution import attribution_label
    return {
        "spill_id": spill_id,
        "vessel_id": row["vessel_id"],
        "mmsi": row["mmsi"],
        "vessel_name": row["name"],
        "vessel_type": row["vessel_type"],
        "flag": row["flag"],
        "final_score": score,
        "label": attribution_label(score),
        "model_version": row["model_version"],
        "disclaimer": "Evidential only. Not a legal finding of culpability.",
    }
