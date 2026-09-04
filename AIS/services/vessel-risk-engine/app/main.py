"""
Vessel Risk Engine Service — FastAPI entry point.

GET /health
GET /vessels/{mmsi}/risk          — full risk score + factor breakdown
GET /vessels/risk                 — list all vessels with optional tier/score filter
POST /risk/recompute              — manual recompute (one or all)
GET /satellite-tasking-requests   — list pending satellite tasking requests
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, "/app")

from shared.db import get_pool, close_pool
from shared.redis_client import get_redis, close_redis
from app.worker import run_risk_worker, STATE, _recompute_risk

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("vessel-risk-engine")
SERVICE_NAME = "vessel-risk-engine"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    pool = await get_pool()
    redis = await get_redis()
    app.state.pool = pool
    app.state.redis = redis
    task = asyncio.create_task(run_risk_worker())
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
    title="Aqua-Sentinel: Vessel Risk Engine",
    description="Explainable weighted risk scoring aggregating anomaly, trust, STS, and dark vessel signals.",
    version="1.0.0",
    lifespan=_lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    hb = STATE["heartbeat"]
    return {
        "status": "ok", "service": SERVICE_NAME,
        "worker_alive": hb is not None and (time.time() - hb) < 90,
        "signals_processed": STATE["signals_processed"],
        "scores_updated": STATE["scores_updated"],
    }


@app.get("/vessels/{mmsi}/risk")
async def get_vessel_risk(mmsi: int):
    """Return the current risk score and factor breakdown for a vessel."""
    pool = app.state.pool
    row = await pool.fetchrow("SELECT * FROM vessel_risk_scores WHERE mmsi=$1", mmsi)
    if not row:
        raise HTTPException(status_code=404, detail=f"No risk score for MMSI {mmsi}")
    result = dict(row)
    if isinstance(result.get("contributing_factors"), str):
        try:
            result["contributing_factors"] = json.loads(result["contributing_factors"])
        except json.JSONDecodeError:
            pass
    if result.get("updated_at"):
        result["updated_at"] = result["updated_at"].isoformat()
    return result


@app.get("/vessels/risk")
async def list_vessel_risks(
    tier: Optional[str] = Query(None),
    min_score: Optional[float] = Query(None, ge=0, le=100),
    limit: int = Query(100, le=500),
):
    """List vessels by risk score, with optional tier and minimum score filters."""
    pool = app.state.pool
    conditions: List[str] = []
    params: List[Any] = []
    idx = 1

    if tier:
        conditions.append(f"tier=${idx}")
        params.append(tier)
        idx += 1
    if min_score is not None:
        conditions.append(f"risk_score >= ${idx}")
        params.append(min_score)
        idx += 1

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)

    rows = await pool.fetch(
        f"SELECT mmsi, risk_score, tier, recommended_action, updated_at "
        f"FROM vessel_risk_scores {where} ORDER BY risk_score DESC LIMIT ${idx}",
        *params,
    )
    return [
        {
            "mmsi": r["mmsi"], "risk_score": r["risk_score"],
            "tier": r["tier"], "recommended_action": r["recommended_action"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


class RecomputeRequest(BaseModel):
    mmsi: Optional[int] = None


@app.post("/risk/recompute")
async def recompute_risk(req: RecomputeRequest):
    """
    Manually trigger risk recomputation.
    If mmsi provided, recomputes that vessel only.
    Otherwise recomputes all vessels seen in the last 24 hours.
    """
    pool = app.state.pool
    redis = app.state.redis

    if req.mmsi:
        await _recompute_risk(req.mmsi, pool, redis)
        return {"status": "done", "mmsi": req.mmsi}

    rows = await pool.fetch(
        "SELECT mmsi FROM vessels WHERE last_seen >= NOW() - INTERVAL '24 hours' LIMIT 500"
    )
    for r in rows:
        asyncio.create_task(_recompute_risk(r["mmsi"], pool, redis))
    return {"status": "recomputing_all", "vessel_count": len(rows)}


@app.get("/satellite-tasking-requests")
async def list_tasking_requests(
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    """Return satellite tasking requests with optional status filter."""
    pool = app.state.pool
    conditions: List[str] = []
    params: List[Any] = []
    idx = 1

    if status:
        conditions.append(f"status=${idx}")
        params.append(status)
        idx += 1

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)

    rows = await pool.fetch(
        f"SELECT id, mmsi, risk_score, risk_tier, reason, requested_at, status "
        f"FROM satellite_tasking_requests {where} ORDER BY requested_at DESC LIMIT ${idx}",
        *params,
    )
    result = []
    for r in rows:
        row = dict(r)
        if isinstance(row.get("reason"), str):
            try:
                row["reason"] = json.loads(row["reason"])
            except json.JSONDecodeError:
                pass
        if row.get("requested_at"):
            row["requested_at"] = row["requested_at"].isoformat()
        result.append(row)
    return result
