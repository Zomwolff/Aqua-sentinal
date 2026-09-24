import asyncio
import json
import logging
import math
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from rasterio.crs import CRS
from rasterio.transform import array_bounds, from_origin
from rasterio.warp import Resampling, calculate_default_transform, reproject

sys.path.insert(0, "/app")

from shared.db.connection import create_pool
from shared.redis_client import close_redis, get_redis, publish_to_stream
from shared.spatial.constants import SENTINEL1_PIXEL_SIZE_M
from app.worker import STATE, SAR_ONNX, SAR_THRESHOLD, run_sar_worker


SERVICE_NAME = "sar-spill-intelligence"

logging.basicConfig(
    level=logging.DEBUG,
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
        "status": "ok" if SAR_ONNX.is_file() else "missing_model",
        "service": SERVICE_NAME,
        "model": "UNet-ResNet34 ONNX",
        "model_ready": SAR_ONNX.is_file(),
        "oil_threshold": SAR_THRESHOLD,
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


_UPLOAD_EXTENSIONS = {".tif", ".tiff"}
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_MAX_RASTER_PIXELS = 100_000_000


def _scene_raster_path(scene_id: str) -> Path:
    root = Path(os.environ.get("SAR_ARTIFACT_ROOT", "artifacts"))
    return root / "sar" / "uploads" / f"{scene_id}.tif"


@app.get("/scenes/{scene_id}/metadata")
async def scene_metadata(scene_id: str):
    raster_path = _scene_raster_path(scene_id)
    if not raster_path.is_file():
        raise HTTPException(status_code=404, detail="No SAR raster found for this scene_id.")

    def _read() -> dict:
        with rasterio.open(raster_path) as src:
            west, south, east, north = src.bounds
            return {
                "scene_id": scene_id,
                "format": "GeoTIFF",
                "crs": src.crs.to_string() if src.crs else None,
                "epsg": src.crs.to_epsg() if src.crs else None,
                "resolution_m": SENTINEL1_PIXEL_SIZE_M,
                "width": src.width,
                "height": src.height,
                "bounds": {"west": west, "south": south, "east": east, "north": north},
                "bands": src.count,
                "band_labels": [description or f"band_{index}"
                                for index, description in enumerate(src.descriptions, 1)],
                "nodata": src.nodata,
            }

    return await asyncio.to_thread(_read)


def _normalise_upload(source_path: Path, output_path: Path, latitude: float, longitude: float) -> dict:
    """Preserve two calibrated SAR bands in source order on the WGS84 map grid."""
    with rasterio.open(source_path) as src:
        if src.count != 2:
            raise ValueError("The SAR UNet requires a two-band calibrated Sigma0-dB GeoTIFF.")
        if src.crs is None:
            raise ValueError("The SAR GeoTIFF needs embedded map coordinates (CRS).")
        if src.width * src.height > _MAX_RASTER_PIXELS:
            raise ValueError("The SAR image exceeds 100 megapixels.")
        if src.crs.to_epsg() == 4326:
            transform, width, height = src.transform, src.width, src.height
            geolocation = "embedded_geotiff"
        else:
            transform, width, height = calculate_default_transform(
                src.crs, CRS.from_epsg(4326), src.width, src.height, *src.bounds
            )
            geolocation = "embedded_geotiff_reprojected"
        bands = np.empty((2, height, width), dtype=np.float32)
        for index in range(1, 3):
            values = np.asarray(src.read(index, masked=True).filled(np.nan), dtype=np.float32)
            if geolocation == "embedded_geotiff":
                bands[index - 1] = values
            else:
                reproject(values, bands[index - 1], src_transform=src.transform,
                          src_crs=src.crs, src_nodata=np.nan,
                          dst_transform=transform, dst_crs="EPSG:4326",
                          dst_nodata=np.nan, resampling=Resampling.bilinear)
        if not np.all(np.isfinite(bands), axis=0).any():
            raise ValueError("The SAR image contains no valid pixels.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(output_path, "w", driver="GTiff", width=width,
                           height=height, count=2, dtype="float32", crs="EPSG:4326",
                           transform=transform, nodata=float("nan"), compress="deflate") as dst:
            dst.write(bands)
            for index, description in enumerate(src.descriptions, 1):
                if description:
                    dst.set_band_description(index, description)
        west, south, east, north = array_bounds(height, width, transform)
    return {
        "width": int(width), "height": int(height), "geolocation": geolocation,
        "radiometry": "provided_sigma0_db", "band_count": 2,
        "bounds": {"west": west, "south": south, "east": east, "north": north},
    }


@app.post("/upload/{mmsi}", status_code=202)
async def upload_sar(mmsi: int, image: UploadFile = File(...)):
    """Persist and enqueue a user SAR image through the production worker."""
    filename = Path(image.filename or "").name
    suffix = Path(filename).suffix.lower()
    log.debug("Upload validation: filename=%s, suffix=%s", filename, suffix)
    if suffix not in _UPLOAD_EXTENSIONS:
        log.debug("Rejected upload: unsupported file extension %s", suffix)
        raise HTTPException(status_code=400, detail="Use a two-band calibrated Sigma0-dB GeoTIFF.")

    payload = await image.read()
    log.debug("Upload validation: payload size=%d bytes", len(payload))
    if not payload:
        log.debug("Rejected upload: empty payload")
        raise HTTPException(status_code=400, detail="The uploaded SAR image is empty.")
    if len(payload) > _MAX_UPLOAD_BYTES:
        log.debug("Rejected upload: payload exceeds max size (%d > %d)", len(payload), _MAX_UPLOAD_BYTES)
        raise HTTPException(status_code=413, detail="SAR uploads are limited to 100 MB.")

    pool = await create_pool()
    vessel = await pool.fetchrow(
        """
        SELECT v.id, v.mmsi, p.latitude, p.longitude,
               rs.risk_score, rs.tier::text AS risk_tier
        FROM vessels v
        JOIN LATERAL (
            SELECT latitude, longitude FROM vessel_positions
            WHERE vessel_id = v.id ORDER BY timestamp DESC LIMIT 1
        ) p ON TRUE
        LEFT JOIN vessel_risk_scores rs ON rs.vessel_id = v.id
        WHERE v.mmsi::text = $1
        """,
        str(mmsi),
    )
    if not vessel:
        raise HTTPException(status_code=404, detail="The selected vessel has no position for georeferencing this image.")

    scene_id = f"USER_SAR_{uuid.uuid4().hex}"
    upload_dir = Path(os.environ.get("SAR_ARTIFACT_ROOT", "artifacts")) / "sar" / "uploads"
    source_path = upload_dir / f"{scene_id}.source{suffix}"
    raster_path = upload_dir / f"{scene_id}.tif"
    tasking_id = None
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(payload)
        raster_info = await asyncio.to_thread(
            _normalise_upload,
            source_path,
            raster_path,
            float(vessel["latitude"]),
            float(vessel["longitude"]),
        )
        if source_path != raster_path:
            source_path.unlink(missing_ok=True)

        reason = {
            "source": "user_upload",
            "original_filename": filename,
            "geolocation": raster_info["geolocation"],
            "radiometry": raster_info["radiometry"],
            "note": "User-provided SAR raster submitted through the dashboard",
        }
        tasking_id = await pool.fetchval(
            """
            INSERT INTO satellite_tasking_requests
                (vessel_id, mmsi, risk_score, risk_tier, reason, status, scene_id)
            VALUES ($1, $2, $3, $4::risk_tier_enum, $5::jsonb, 'processing', $6)
            RETURNING id
            """,
            vessel["id"], str(vessel["mmsi"]), vessel["risk_score"],
            vessel["risk_tier"], json.dumps(reason), scene_id,
        )

        metadata = {
            "scene_id": scene_id,
            "acquisition_time": datetime.now(timezone.utc).isoformat(),
            "source": "user_upload",
            "is_synthetic": False,
            "polarization": "two_band_source_order_unverified",
            "resolution": 10,
            "mmsi": str(vessel["mmsi"]),
            "geolocation": raster_info["geolocation"],
            "radiometry": raster_info["radiometry"],
            "band_count": raster_info["band_count"],
        }
        redis = await get_redis()
        await publish_to_stream(redis, "sar.clean", {
            "scene_metadata": json.dumps(metadata),
            "raster_path": str(raster_path),
        })
        return {
            "accepted": True,
            "tasking_id": tasking_id,
            "scene_id": scene_id,
            "mmsi": str(vessel["mmsi"]),
            "status": "processing",
            "geolocation": raster_info["geolocation"],
            "radiometry": raster_info["radiometry"],
            "band_count": raster_info["band_count"],
            "bounds": raster_info["bounds"],
        }
    except HTTPException:
        raise
    except Exception as exc:
        source_path.unlink(missing_ok=True)
        raster_path.unlink(missing_ok=True)
        if tasking_id is not None:
            await pool.execute(
                "UPDATE satellite_tasking_requests SET status='failed', completed_at=NOW() WHERE id=$1",
                tasking_id,
            )
        log.exception("User SAR upload failed for MMSI %s", mmsi)
        raise HTTPException(status_code=400, detail=f"Unable to read this SAR image: {exc}")
