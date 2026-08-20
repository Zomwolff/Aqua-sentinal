import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, "/app")

from shared.redis_client import close_redis, get_redis
from app.worker import STATE, run_sar_worker


SERVICE_NAME = "sar-spill-intelligence"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.redis = await get_redis()
    task = asyncio.create_task(run_sar_worker())
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
        "messages_consumed": STATE["messages_consumed"],
        "scenes_processed": STATE["scenes_processed"],
        "scenes_failed": STATE["scenes_failed"],
    }


@app.get("/status")
async def status():
    """Read-only status; filtered image and mask are never serialized here."""
    return {
        "last_scene_id": STATE["last_scene_id"],
        "last_processed_at": STATE["last_processed_at"],
        "last_raster_metadata": STATE["last_raster_metadata"],
    }
