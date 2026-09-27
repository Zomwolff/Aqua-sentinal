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
from app.origin_api import router as origin_router
from app.origin_worker import run_origin_worker

SERVICE_NAME = "drift-forecast"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await create_pool()
    await get_redis()
    task = asyncio.create_task(run_drift_worker())
    origin_task = asyncio.create_task(run_origin_worker())
    app.state.origin_worker_task = origin_task
    app.state.worker_task = task
    log.info("%s started.", SERVICE_NAME)
    yield
    task.cancel()
    origin_task.cancel()
    try:
        await origin_task
    except asyncio.CancelledError:
        pass
    try:
        await task
    except asyncio.CancelledError:
        pass
    await close_pool()
    await close_redis()


app = FastAPI(title=SERVICE_NAME, version="1.0.0", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

app.include_router(origin_router)


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
    """All forecast horizons for a spill, with GeoJSON geometries and V2 separated physics metrics."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT horizon_hours, forecast_time, generated_at,
               confidence, model_version,
               ST_AsGeoJSON(geom)::text AS geom_json,
               ST_AsGeoJSON(probability_50_geom)::text AS prob50_json,
               ST_AsGeoJSON(probability_90_geom)::text AS prob90_json,
               ST_Area(geom::geography) AS area_m2,
               drift_distance_m, drift_velocity_ms, drift_bearing_deg,
               physical_area_m2, physical_radius_m, expansion_ratio, spread_rate_m2_per_hour,
               uncertainty_rms_m, uncertainty_std_east_m, uncertainty_std_north_m
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
            "probability_50_geometry": _json.loads(r["prob50_json"]) if r["prob50_json"] else None,
            "probability_90_geometry": _json.loads(r["prob90_json"]) if r["prob90_json"] else None,
            # V2 drift metrics
            "drift_distance_m": float(r["drift_distance_m"]) if r["drift_distance_m"] else None,
            "drift_velocity_ms": float(r["drift_velocity_ms"]) if r["drift_velocity_ms"] else None,
            "drift_bearing_deg": float(r["drift_bearing_deg"]) if r["drift_bearing_deg"] else None,
            # V2 physical spreading metrics
            "physical_area_m2": float(r["physical_area_m2"]) if r["physical_area_m2"] else None,
            "physical_radius_m": float(r["physical_radius_m"]) if r["physical_radius_m"] else None,
            "expansion_ratio": float(r["expansion_ratio"]) if r["expansion_ratio"] else None,
            "spread_rate_m2_per_hour": float(r["spread_rate_m2_per_hour"]) if r["spread_rate_m2_per_hour"] else None,
            # V2 uncertainty metrics
            "uncertainty_rms_m": float(r["uncertainty_rms_m"]) if r["uncertainty_rms_m"] else None,
            "uncertainty_std_east_m": float(r["uncertainty_std_east_m"]) if r["uncertainty_std_east_m"] else None,
            "uncertainty_std_north_m": float(r["uncertainty_std_north_m"]) if r["uncertainty_std_north_m"] else None,
        }
        for r in rows
    ])
