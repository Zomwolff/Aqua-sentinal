"""
Severity Impact Service — FastAPI entry point.
"""
from __future__ import annotations
import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
sys.path.insert(0, "/app")
from shared.db.connection import close_pool, create_pool, get_pool
from shared.redis_client import close_redis, get_redis
from app.worker import STATE, run_severity_worker

SERVICE_NAME = "severity-impact"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(SERVICE_NAME)

@asynccontextmanager
async def _lifespan(app: FastAPI):
    await create_pool()
    await get_redis()
    task = asyncio.create_task(run_severity_worker())
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
        **{k: STATE[k] for k in ("spills_processed", "failed", "last_spill_id")},
    }

@app.get("/severity/{spill_id}")
async def get_severity(
    spill_id: str,
    horizon_hours: Optional[float] = None,
    footprint_type: Optional[str] = None,
):
    """
    Get severity assessment for a spill.
    
    Query parameters:
    - horizon_hours: Filter by forecast horizon (1, 3, 6, 12, 24). Omit for overall/current.
    - footprint_type: Filter by footprint type (best_estimate, probability_90, current)
    
    Without filters, returns the overall severity summary plus all horizon-specific records.
    """
    pool = get_pool()
    
    conditions = ["spill_id = $1"]
    params = [spill_id]
    idx = 2
    
    if horizon_hours is not None:
        conditions.append(f"horizon_hours = ${idx}")
        params.append(horizon_hours)
        idx += 1
    
    if footprint_type:
        conditions.append(f"footprint_type = ${idx}")
        params.append(footprint_type)
        idx += 1
    
    where = " AND ".join(conditions)
    
    rows = await pool.fetch(
        f"""
        SELECT * FROM severity 
        WHERE {where}
        ORDER BY COALESCE(horizon_hours, 0) ASC, footprint_type ASC, computed_at DESC
        """,
        *params,
    )
    
    if not rows:
        raise HTTPException(status_code=404, detail="No severity records for spill_id")
    
    def _parse_json(val):
        if val is None:
            return None
        if isinstance(val, str):
            try:
                return json.loads(val)
            except:
                return val
        return val
    
    return JSONResponse([
        {
            "id": r["id"],
            "spill_id": str(spill_id),
            "severity_level": r["severity_level"],
            "score": float(r["score"]),
            
            # V1 fields
            "horizon_hours": float(r["horizon_hours"]) if r["horizon_hours"] is not None else None,
            "footprint_type": r["footprint_type"],
            "methodology_version": r.get("methodology_version"),
            
            "peak_severity": r.get("peak_severity"),
            "peak_horizon_hours": float(r["peak_horizon_hours"]) if r.get("peak_horizon_hours") else None,
            "peak_footprint_type": r.get("peak_footprint_type"),
            "peak_score": float(r["peak_score"]) if r.get("peak_score") is not None else None,
            
            "trajectory": r.get("trajectory"),
            
            "ecological_severity": r.get("ecological_severity"),
            "socioeconomic_severity": r.get("socioeconomic_severity"),
            
            "escalation_applied": r.get("escalation_applied"),
            "escalation_reasons": r.get("escalation_reasons"),
            "primary_drivers": _parse_json(r.get("primary_drivers")),
            
            "ttfe_hours": float(r["ttfe_hours"]) if r.get("ttfe_hours") is not None else None,
            "affected_receptor_count": int(r["affected_receptor_count"]) if r.get("affected_receptor_count") is not None else None,
            
            "ports_within_5km": int(r["ports_within_5km"]) if r.get("ports_within_5km") is not None else None,
            "fishing_zones_within_10km": int(r["fishing_zones_within_10km"]) if r.get("fishing_zones_within_10km") is not None else None,
            
            "physical_area_m2": float(r["physical_area_m2"]) if r.get("physical_area_m2") is not None else None,
            "drift_distance_m": float(r["drift_distance_m"]) if r.get("drift_distance_m") is not None else None,
            "expansion_ratio": float(r["expansion_ratio"]) if r.get("expansion_ratio") is not None else None,
            
            # Legacy compatibility fields (kept for Response-Decision)
            "environmental_risk": float(r["environmental_risk"]) if r.get("environmental_risk") is not None else None,
            "population_risk": float(r["population_risk"]) if r.get("population_risk") is not None else None,
            "economic_risk": float(r["economic_risk"]) if r.get("economic_risk") is not None else None,
            "protected_area_risk": float(r["protected_area_risk"]) if r.get("protected_area_risk") is not None else None,
            
            "computed_at": r["computed_at"].isoformat() if r["computed_at"] else None,
        }
        for r in rows
    ])
