"""Response Decision Service - FastAPI entry point."""
from __future__ import annotations
import asyncio, logging, sys, time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
sys.path.insert(0, "/app")
from shared.db.connection import close_pool, create_pool, get_pool
from shared.redis_client import close_redis, get_redis
from app.worker import STATE, run_response_worker

SERVICE_NAME = "response-decision"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await create_pool()
    await get_redis()
    task = asyncio.create_task(run_response_worker())
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
        **{k: STATE[k] for k in ("spills_processed", "recommendations_written", "failed", "last_spill_id")},
    }


@app.get("/recommendations/{spill_id}")
async def get_recommendations(spill_id: str):
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, recommendation, priority, status, generated_at,
               acknowledged_at, acknowledged_by
        FROM response_recommendations
        WHERE spill_id = $1
        ORDER BY
            CASE priority
                WHEN 'URGENT' THEN 1 WHEN 'HIGH' THEN 2
                WHEN 'MEDIUM' THEN 3 ELSE 4
            END, generated_at ASC
        """, spill_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No recommendations for spill_id")
    return JSONResponse([
        {
            "id": r["id"], "recommendation": r["recommendation"],
            "priority": r["priority"], "status": r["status"],
            "generated_at": r["generated_at"].isoformat() if r["generated_at"] else None,
            "acknowledged_at": r["acknowledged_at"].isoformat() if r["acknowledged_at"] else None,
            "acknowledged_by": r["acknowledged_by"],
        }
        for r in rows
    ])


@app.patch("/recommendations/{recommendation_id}/acknowledge")
async def acknowledge_recommendation(recommendation_id: int, acknowledged_by: str = "operator"):
    pool = get_pool()
    row = await pool.fetchrow(
        """
        UPDATE response_recommendations
        SET status='acknowledged', acknowledged_at=NOW(), acknowledged_by=$2
        WHERE id=$1
        RETURNING id, spill_id, status
        """,
        recommendation_id, acknowledged_by,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return {"id": row["id"], "spill_id": str(row["spill_id"]), "status": row["status"]}
