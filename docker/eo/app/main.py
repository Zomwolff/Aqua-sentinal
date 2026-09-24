"""
EO (Earth Observation / optical satellite) service — FastAPI entry point.

This is the genuinely-trained model of the three "ML" containers (ais / sar /
eo). It wraps `optical imagery/sentinel2_interface.py` and the included
checkpoint (`sentinel2_cnn_10band_best.pth`) — a CNN trained on the MADOS
Sentinel-2 dataset, 10 spectral bands in, 15-class softmax out (class 6 =
"Oil Spill"). This legacy EO service is optional; the active fusion service
uses the binary EO ONNX model alongside the SAR UNet-ResNet34 ONNX model.

Positioning in the pipeline (per the project's own docs/context-files/
oil-context.md): SAR is the primary detector; optical is SECONDARY,
NON-MANDATORY verification, because clouds can block it entirely. So this
service:

  - subscribes to `spill.candidates.filtered` (post-lookalike-engine, i.e.
    only candidates that already survived the oil-vs-lookalike filter),
  - looks up each candidate's centroid + acquisition time,
  - searches Sentinel-2 for a cloud-free scene near that location/time
    (gee_optical.find_scene) — if none exists within the window, it silently
    records "not checked" and moves on, exactly per the "don't make
    Sentinel-2 mandatory" design note,
  - otherwise exports+downloads the 10-band stack and runs the CNN,
  - writes the result onto the candidate's own row in Postgres
    (optical_oil_probability / optical_predicted_class / ...), and
  - publishes `optical.confirmed` so evidence-fusion can react (fusion
    itself already happened without waiting — this is late enrichment).

Also exposes a manual upload path (`POST /classify/upload`) for testing or
demo use without going through GEE at all — mirrors the SAR service's own
manual-upload endpoint (`POST /upload/{mmsi}`).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, "/svc/eo")
sys.path.insert(0, "/svc/eo/model")

from shared.db.connection import create_pool  # noqa: E402
from shared.redis_client import (  # noqa: E402
    consume_stream,
    ensure_consumer_group,
    get_redis,
    publish_to_stream,
)
from app import gee_optical  # noqa: E402
from sentinel2_interface import get_data, get_fusion_output  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("eo")

SERVICE_NAME = "eo"
CONSUMER_GROUP = "eo"
FILTERED_STREAM = "spill.candidates.filtered"
OPTICAL_STREAM = "optical.confirmed"

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "candidates_seen": 0,
    "cloud_free_found": 0,
    "no_cloud_free_scene": 0,
    "classifications_run": 0,
    "errors": 0,
}


async def _fetch_candidate(pool, candidate_id: str) -> Optional[Dict[str, Any]]:
    row = await pool.fetchrow(
        """
        SELECT ST_X(ST_Centroid(geom)) AS lon, ST_Y(ST_Centroid(geom)) AS lat,
               acquisition_time
        FROM spill_candidates WHERE candidate_id = $1
        """,
        candidate_id,
    )
    return dict(row) if row else None


async def _mark_not_checked(pool, candidate_id: str) -> None:
    await pool.execute(
        """
        UPDATE spill_candidates
        SET optical_cloud_free = FALSE, optical_checked_at = NOW()
        WHERE candidate_id = $1
        """,
        candidate_id,
    )


async def _store_result(pool, candidate_id: str, scene_id: str, result: Dict[str, Any]) -> None:
    await pool.execute(
        """
        UPDATE spill_candidates
        SET optical_oil_probability = $2,
            optical_predicted_class = $3,
            optical_class_probabilities = $4::jsonb,
            optical_scene_id = $5,
            optical_cloud_free = TRUE,
            optical_checked_at = NOW()
        WHERE candidate_id = $1
        """,
        candidate_id,
        result["oil_probability"],
        result["predicted_class_name"],
        __import__("json").dumps(result["class_probabilities"]),
        scene_id,
    )


async def _process_candidate(data: Dict[str, Any], pool, redis) -> None:
    candidate_id = str(data["candidate_id"])
    STATE["candidates_seen"] += 1

    row = await _fetch_candidate(pool, candidate_id)
    if row is None or row["lat"] is None or row["lon"] is None or row["acquisition_time"] is None:
        log.warning("eo: candidate %s missing geometry/time, skipping", candidate_id)
        return

    try:
        scene = await asyncio.to_thread(
            gee_optical.find_scene, row["lat"], row["lon"], row["acquisition_time"],
        )
    except Exception:
        log.exception("eo: Sentinel-2 search failed for candidate %s", candidate_id)
        STATE["errors"] += 1
        return

    if scene is None:
        STATE["no_cloud_free_scene"] += 1
        await _mark_not_checked(pool, candidate_id)
        return  # silent, expected — see module docstring

    STATE["cloud_free_found"] += 1
    try:
        tif_path = await asyncio.to_thread(gee_optical.export_and_download, scene, candidate_id)
        s2data = await asyncio.to_thread(get_data, tif_path)
        result = await asyncio.to_thread(get_fusion_output, s2data)
    except Exception:
        log.exception("eo: export/inference failed for candidate %s", candidate_id)
        STATE["errors"] += 1
        return

    await _store_result(pool, candidate_id, scene["id"], result)
    STATE["classifications_run"] += 1
    await publish_to_stream(redis, OPTICAL_STREAM, {
        "candidate_id": candidate_id,
        "scene_id": scene["id"],
        "cloud_free": True,
        "oil_probability": result["oil_probability"],
        "predicted_class": result["predicted_class_name"],
    })
    log.info(
        "eo: candidate=%s scene=%s oil_probability=%.3f class=%s",
        candidate_id, scene["id"], result["oil_probability"], result["predicted_class_name"],
    )


async def _worker_loop() -> None:
    gee_configured = bool(os.getenv("GEE_SERVICE_ACCOUNT")) and bool(os.getenv("GEE_PRIVATE_KEY_PATH"))
    if not gee_configured:
        log.warning("eo: GEE_SERVICE_ACCOUNT/GEE_PRIVATE_KEY_PATH not set — "
                    "worker will idle (manual /classify/upload still works)")
        while True:
            STATE["heartbeat"] = time.time()
            await asyncio.sleep(30)

    gee_optical.init_gee()
    pool = await create_pool()
    redis = await get_redis()
    await ensure_consumer_group(redis, FILTERED_STREAM, CONSUMER_GROUP)
    log.info("eo worker started, consuming %s", FILTERED_STREAM)

    while True:
        STATE["heartbeat"] = time.time()
        messages = await consume_stream(redis, FILTERED_STREAM, CONSUMER_GROUP, "eo-worker", count=10, block_ms=5000)
        for message in messages:
            try:
                await _process_candidate(message["data"], pool, redis)
            except Exception:
                log.exception("eo: unhandled error processing message")
                STATE["errors"] += 1


@asynccontextmanager
async def _lifespan(app: FastAPI):
    task = asyncio.create_task(_worker_loop())
    yield
    task.cancel()


app = FastAPI(title="EO (Optical Satellite) Service", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME, **STATE}


@app.post("/classify/upload")
async def classify_upload(file: UploadFile = File(...)):
    """Manual path: upload a 10-band GeoTIFF (training-order bands) and get
    back oil probability + class, with no GEE/Redis/Postgres involved. Useful
    for testing the model or a live demo without waiting on a real SAR
    candidate + Sentinel-2 availability."""
    dest_dir = Path("/tmp/eo-uploads")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / file.filename
    with open(dest_path, "wb") as f:
        f.write(await file.read())
    try:
        data = get_data(str(dest_path))
        result = get_fusion_output(data)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result
