"""
Severity Impact Service — FastAPI entry point.
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
async def get_severity(spill_id: str):
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT * FROM severity WHERE spill_id = $1 ORDER BY computed_at DESC",
        spill_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No severity records for spill_id")
    return JSONResponse([
        {
            "id": r["id"],
            "spill_id": str(spill_id),
            "severity_level": r["severity_level"],
            "score": float(r["score"]),
            "environmental_risk": float(r["environmental_risk"]) if r["environmental_risk"] else None,
            "population_risk": float(r["population_risk"]) if r["population_risk"] else None,
            "economic_risk": float(r["economic_risk"]) if r["economic_risk"] else None,
            "protected_area_risk": float(r["protected_area_risk"]) if r["protected_area_risk"] else None,
            "computed_at": r["computed_at"].isoformat() if r["computed_at"] else None,
        }
        for r in rows
    ])
