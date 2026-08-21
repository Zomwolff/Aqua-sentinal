"""
Data Ingestion Service — FastAPI application entry point.

Provides:
  GET  /health               — liveness + worker heartbeat
  GET  /ingest/status        — recent ingestion statistics
  POST /ingest/ais           — manual AIS record injection (batch or single)
  GET  /reference-layers     — list loaded reference layers
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Add /app to sys.path so 'shared' and 'app' are importable as top-level packages
sys.path.insert(0, "/app")

from shared.db import get_pool, close_pool
from shared.redis_client import get_redis, close_redis
from app.worker import run_ingestion_worker, ingest_ais_batch, STATE
from app.dynamic_sar_worker import dynamic_sar_worker
from app.deduplicator import AISDeduplicator
from app.reference_loader import load_reference_layers, get_port_polygons, get_protected_zones

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("data-ingestion")

SERVICE_NAME = "data-ingestion"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup: initialise DB, Redis, reference layers, then launch worker."""
    log.info("Starting %s ...", SERVICE_NAME)
    try:
        pool = await get_pool()
        redis = await get_redis()
        app.state.pool = pool
        app.state.redis = redis
        app.state.deduplicator = AISDeduplicator(redis)

        # Load geospatial reference layers (ports, coastline, etc.)
        await load_reference_layers(pool)

        # Start live AIS ingestion worker
        task = asyncio.create_task(run_ingestion_worker())
        app.state.worker_task = task
        
        # Start dynamic SAR tasking worker
        sar_task = asyncio.create_task(dynamic_sar_worker())
        app.state.sar_task = sar_task
        log.info("%s startup complete.", SERVICE_NAME)
    except Exception as exc:
        log.exception("Startup failed: %s", exc)
        raise

    yield  # ──── service is running ────

    log.info("Shutting down %s ...", SERVICE_NAME)
    if hasattr(app.state, "worker_task"):
        app.state.worker_task.cancel()
        try:
            await app.state.worker_task
        except asyncio.CancelledError:
            pass
            
    if hasattr(app.state, "sar_task"):
        app.state.sar_task.cancel()
        try:
            await app.state.sar_task
        except asyncio.CancelledError:
            pass
            
    await close_pool()
    await close_redis()


app = FastAPI(
    title="Aqua-Sentinel: Data Ingestion",
    description="Live AIS ingestion, validation, deduplication and normalisation.",
    version="1.0.0",
    lifespan=_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Returns service health.
    worker_alive is True only if the worker heartbeat was updated in the last 60 seconds.
    This distinguishes "HTTP server is up but worker crashed" from truly healthy.
    """
    heartbeat = STATE["heartbeat"]
    worker_alive = heartbeat is not None and (time.time() - heartbeat) < 60
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "worker_alive": worker_alive,
        "provider": STATE["provider"],
        "last_message_at": STATE["last_message_at"],
        "messages_processed": STATE["messages_processed"],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Ingestion status
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/ingest/status")
async def ingest_status():
    """Current ingestion statistics — accepted/rejected counts and reasons."""
    return {
        "provider": STATE["provider"],
        "started_at": STATE["started_at"],
        "messages_processed": STATE["messages_processed"],
        "messages_rejected": STATE["messages_rejected"],
        "rejection_reasons": dict(STATE["rejection_reasons"]),
        "last_message_at": STATE["last_message_at"],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Manual AIS injection
# ──────────────────────────────────────────────────────────────────────────────

class AISIngestRequest(BaseModel):
    mmsi: int
    lat: float
    lon: float
    speed_knots: Optional[float] = None
    course: Optional[float] = None
    heading: Optional[int] = None
    nav_status: Optional[int] = None
    timestamp: str  # ISO-8601
    vessel_name: Optional[str] = None
    vessel_type: Optional[int] = None
    vessel_type_str: Optional[str] = None
    imo_number: Optional[int] = None
    call_sign: Optional[str] = None
    quality_flag: str = "raw"
    raw_source: str = "http_post"


@app.post("/ingest/ais")
async def ingest_ais(request: Request):
    """
    Accepts a single AIS record (dict) or a batch (list of dicts).
    Runs each through the full validate → dedup → DB pipeline.
    Returns {accepted, rejected, rejection_reasons}.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if isinstance(body, dict):
        records = [body]
    elif isinstance(body, list):
        records = body
    else:
        raise HTTPException(status_code=400, detail="Body must be a JSON object or array")

    # Normalise timestamps
    from app.normalizer import normalize_timestamp
    for rec in records:
        if isinstance(rec.get("timestamp"), str):
            ts = normalize_timestamp(rec["timestamp"])
            if ts:
                rec["timestamp"] = ts

    result = await ingest_ais_batch(
        records,
        request.app.state.pool,
        request.app.state.redis,
        request.app.state.deduplicator,
    )
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Reference layers
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/reference-layers")
async def list_reference_layers(
    layer_type: Optional[str] = Query(None, description="Filter by type: port | coastline | protected_zone | eez"),
    request: Request = None,
):
    """Return loaded reference layers."""
    pool = request.app.state.pool
    try:
        if layer_type:
            rows = await pool.fetch(
                "SELECT id, layer_type, name FROM reference_layers WHERE layer_type=$1 LIMIT 500",
                layer_type,
            )
        else:
            rows = await pool.fetch(
                "SELECT id, layer_type, name FROM reference_layers LIMIT 500"
            )
        return [{"id": r["id"], "layer_type": r["layer_type"], "name": r["name"]} for r in rows]
    except Exception as e:
        log.error("reference-layers query failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
