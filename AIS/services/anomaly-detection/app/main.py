"""
Anomaly Detection Service — FastAPI application entry point.
GET /health
GET /anomalies             — filter by mmsi, severity, since
GET /anomalies/{event_id}
POST /anomalies/evaluate   — manual re-evaluation trigger
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, "/app")

from shared.db import get_pool, close_pool
from shared.redis_client import get_redis, close_redis
from app.worker import run_anomaly_worker, STATE, _process_features

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("anomaly-detection")
SERVICE_NAME = "anomaly-detection"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    pool = await get_pool()
    redis = await get_redis()
    app.state.pool = pool
    app.state.redis = redis
    task = asyncio.create_task(run_anomaly_worker())
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
    title="Aqua-Sentinel: Anomaly Detection",
    description="Rule-based and statistical anomaly detection on AIS behavioral features.",
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
        "features_consumed": STATE["features_consumed"],
        "anomalies_emitted": STATE["anomalies_emitted"],
        "last_gap_check_at": STATE["last_gap_check_at"],
    }


@app.get("/anomalies")
async def list_anomalies(
    mmsi: Optional[int] = Query(None),
    severity: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
    pool = app.state.pool
    conditions: List[str] = []
    params: List[Any] = []
    idx = 1

    if mmsi:
        conditions.append(f"mmsi=${idx}"); params.append(mmsi); idx += 1
    if severity:
        conditions.append(f"severity=${idx}"); params.append(severity); idx += 1
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid 'since' datetime")
        conditions.append(f"window_start >= ${idx}"); params.append(since_dt); idx += 1
    else:
        conditions.append(f"window_start >= ${idx}")
        params.append(datetime.now(timezone.utc) - timedelta(hours=24))
        idx += 1

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)
    rows = await pool.fetch(
        f"SELECT id, mmsi, window_start, anomaly_type, severity, evidence "
        f"FROM anomaly_events {where} ORDER BY window_start DESC LIMIT ${idx}",
        *params,
    )
    result = []
    for r in rows:
        row = dict(r)
        if isinstance(row.get("evidence"), str):
            try:
                row["evidence"] = json.loads(row["evidence"])
            except json.JSONDecodeError:
                pass
        result.append(row)
    return result


@app.get("/anomalies/{event_id}")
async def get_anomaly(event_id: int):
    pool = app.state.pool
    row = await pool.fetchrow("SELECT * FROM anomaly_events WHERE id=$1", event_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Anomaly event {event_id} not found")
    result = dict(row)
    if isinstance(result.get("evidence"), str):
        try:
            result["evidence"] = json.loads(result["evidence"])
        except json.JSONDecodeError:
            pass
    return result


class EvaluateRequest(BaseModel):
    mmsi: Optional[int] = None


@app.post("/anomalies/evaluate")
async def evaluate_anomalies(req: EvaluateRequest):
    """Manual re-evaluation: reads latest vessel_features from DB and runs full detection."""
    pool = app.state.pool
    redis = app.state.redis

    if req.mmsi:
        row = await pool.fetchrow(
            "SELECT * FROM vessel_features WHERE mmsi=$1 ORDER BY window_end DESC LIMIT 1",
            req.mmsi,
        )
        if not row:
            raise HTTPException(status_code=404, detail=f"No features for MMSI {req.mmsi}")
        rows = [row]
    else:
        rows = await pool.fetch(
            "SELECT DISTINCT ON (mmsi) * FROM vessel_features ORDER BY mmsi, window_end DESC LIMIT 200"
        )

    triggered = 0
    for row in rows:
        features = dict(row)
        if isinstance(features.get("proximity_events"), str):
            try:
                features["proximity_events"] = json.loads(features["proximity_events"])
            except json.JSONDecodeError:
                features["proximity_events"] = []
        asyncio.create_task(_process_features(features, pool, redis))
        triggered += 1

    return {"status": "evaluating", "vessel_count": triggered}
