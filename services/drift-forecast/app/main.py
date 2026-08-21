"""
Drift Forecast Service — FastAPI entry point.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

sys.path.insert(0, "/app")
from shared.db.connection import close_pool, create_pool, get_pool
from shared.redis_client import close_redis, get_redis
from app.worker import STATE, run_drift_worker

SERVICE_NAME = "drift-forecast"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await create_pool()
    await get_redis()
    task = asyncio.create_task(run_drift_worker())
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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    hb = STATE["heartbeat"]
    return {
        "status": "ok" if hb and (time.time() - hb) < 30 else "degraded",
        "service": SERVICE_NAME,
        "worker_alive": hb is not None and (time.time() - hb) < 30,
        **{k: STATE[k] for k in ("spills_processed", "forecasts_written", "failed", "last_spill_id")},
    }


@app.get("/forecasts/{spill_id}")
async def get_forecasts(spill_id: str):
    """All forecast horizons for a spill, with GeoJSON geometries."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT horizon_hours, forecast_time, generated_at,
               confidence, model_version,
               ST_AsGeoJSON(geom)::text AS geom_json,
               ST_Area(geom::geography) AS area_m2
        FROM forecasts
        WHERE spill_id = $1
        ORDER BY horizon_hours ASC
        """,
        spill_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No forecasts for spill_id")
    import json as _json
    return JSONResponse([
        {
            "horizon_hours": float(r["horizon_hours"]),
            "forecast_time": r["forecast_time"].isoformat() if r["forecast_time"] else None,
            "generated_at":  r["generated_at"].isoformat()  if r["generated_at"] else None,
            "confidence":    float(r["confidence"]) if r["confidence"] is not None else None,
            "model_version": r["model_version"],
            "area_m2":       float(r["area_m2"]) if r["area_m2"] else None,
            "geometry":      _json.loads(r["geom_json"]) if r["geom_json"] else None,
        }
        for r in rows
    ])
