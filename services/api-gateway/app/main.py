import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

SERVICE_NAME = "api-gateway"

_state = {"heartbeat": None}


async def _worker() -> None:
    print(f"[{SERVICE_NAME}] worker started")
    while True:
        _state["heartbeat"] = time.time()
        await asyncio.sleep(5)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    task = asyncio.create_task(_worker())
    yield
    task.cancel()


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
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "worker_alive": _state["heartbeat"] is not None,
    }


@app.get("/incidents")
async def incidents():
    return []


@app.websocket("/live")
async def live(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json(
                {
                    "type": "heartbeat",
                    "service": SERVICE_NAME,
                    "timestamp": time.time(),
                }
            )
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        pass
