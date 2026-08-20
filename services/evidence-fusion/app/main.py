import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, "/app")

from shared.redis_client import close_redis, get_redis
from app.worker import STATE, run_evidence_worker


SERVICE_NAME = "evidence-fusion"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.redis = await get_redis()
    task = asyncio.create_task(run_evidence_worker())
    app.state.worker_task = task
    log.info("%s started.", SERVICE_NAME)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await close_redis()


app = FastAPI(title=SERVICE_NAME, lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    heartbeat = STATE["heartbeat"]
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "worker_alive": heartbeat is not None and (time.time() - heartbeat) < 90,
        "vessel_risk_messages": STATE["vessel_risk_messages"],
        "filtered_messages": STATE["filtered_messages"],
        "candidates_fused": STATE["candidates_fused"],
        "candidates_with_vessel": STATE["candidates_with_vessel"],
        "scenes_failed": STATE["scenes_failed"],
    }


@app.get("/status")
async def status():
    """Read-only status — no attribution claims."""
    return {
        "last_candidate_id": STATE["last_candidate_id"],
        "last_processed_at": STATE["last_processed_at"],
    }