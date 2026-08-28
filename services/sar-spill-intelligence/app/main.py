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
from app.worker import STATE, run_sar_worker


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


_UPLOAD_EXTENSIONS = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_MAX_RASTER_PIXELS = 100_000_000


def _scene_raster_path(scene_id: str) -> Path:
    """Locate a scene's normalised raster the same way the worker reads it.

    Uploads always land at ``<root>/sar/uploads/<scene_id>.tif`` (see
    ``upload_sar`` below); this mirrors that convention rather than adding a
    second one, so a scene's metadata always reflects the exact file the
    worker actually processed.
    """
    root = Path(os.environ.get("SAR_ARTIFACT_ROOT", "artifacts"))
    return root / "sar" / "uploads" / f"{scene_id}.tif"


@app.get("/scenes/{scene_id}/metadata")
async def scene_metadata(scene_id: str):
    """Read-only SAR raster metadata for the frontend's SAR Image Information panel.

    Values are read live from the normalised (always-EPSG:4326) GeoTIFF on
    the shared artifact volume, so this reflects reality even if a scene was
    reprojected from a non-4326 source at upload time.
    """
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
                # The pipeline is calibrated for, and this normalisation step
                # always targets, Sentinel-1's 10 m ground-range pixel size —
                # reported directly rather than re-derived from the (now
                # degree-based, EPSG:4326) pixel size, which would vary with
                # latitude and not reflect the sensor's actual resolution.
                "resolution_m": SENTINEL1_PIXEL_SIZE_M,
                "width": src.width,
                "height": src.height,
                "bounds": {"west": west, "south": south, "east": east, "north": north},
                "bands": src.count,
                # Band-1-only convention (see _normalise_upload docstring):
                # for a multi-band source this is always the first band,
                # treated as VV for a dual-pol Sentinel-1 product.
                "band_labels": ["VV"] if src.count >= 1 else [],
                "nodata": src.nodata,
            }

    return await asyncio.to_thread(_read)


def _normalise_upload(source_path: Path, output_path: Path, latitude: float, longitude: float) -> dict:
    """Write a single-band EPSG:4326 GeoTIFF suitable for the existing worker.

    Band selection: when the source is a real georeferenced GeoTIFF with more
    than one band (e.g. a Sentinel-1 dual-pol VV+VH product), band 1 is always
    used — for Sentinel-1 GRD products this is conventionally VV, which is the
    polarisation the rest of this pipeline (despeckle/CFAR/dark-region
    thresholds) is tuned against. There is no per-request band selection; if a
    future need arises to process VH (or another band) it should be added as
    an explicit "band" upload parameter rather than inferred from the file.

    CRS handling: a GeoTIFF with an embedded CRS other than EPSG:4326 (for
    example a Sentinel-1 product delivered in a UTM zone) is reprojected to
    EPSG:4326 with bilinear resampling rather than rejected, so the frontend
    map and the rest of the pipeline can always assume WGS84 lon/lat.

    NoData handling: pixels flagged by the source GeoTIFF's `nodata` value (or
    otherwise non-finite) are read as NaN, carried through reprojection as
    NaN, and the *output* file has `nodata=NaN` set explicitly. This is the
    piece that previously went missing between upload and processing: without
    it, a source file's NoData value was silently dropped before the worker's
    masked read ever saw it, so a sea-boundary/border NoData pixel could be
    scaled into a false dark-region candidate. PNG/JPEG uploads have no
    NoData concept and are not masked.
    """
    with rasterio.open(source_path) as src:
        if src.count < 1:
            raise ValueError("The SAR image has no raster bands.")
        if src.width * src.height > _MAX_RASTER_PIXELS:
            raise ValueError("The SAR image is too large; maximum raster size is 100 megapixels.")
        band_count = src.count

        if not src.crs and src.count >= 3:
            rgb = src.read((1, 2, 3)).astype("float32")
            image = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
        elif not src.crs:
            image = src.read(1).astype("float32")
        else:
            # Always band 1 (see docstring); read masked so the source's
            # NoData value (whatever it is: 0, -9999, a real NaN, ...) becomes
            # a proper NaN instead of a plain pixel value.
            masked = src.read(1, masked=True)
            image = np.asarray(masked.filled(np.nan), dtype="float32")

        if not np.isfinite(image).any():
            raise ValueError("The SAR image contains no usable pixels.")

        if src.crs:
            if src.crs.to_epsg() != 4326:
                dst_crs = CRS.from_epsg(4326)
                dst_transform, dst_width, dst_height = calculate_default_transform(
                    src.crs, dst_crs, src.width, src.height, *src.bounds
                )
                reprojected = np.full((dst_height, dst_width), np.nan, dtype="float32")
                reproject(
                    source=image,
                    destination=reprojected,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    src_nodata=np.nan,
                    dst_transform=dst_transform,
                    dst_crs=dst_crs,
                    dst_nodata=np.nan,
                    resampling=Resampling.bilinear,
                )
                image = reprojected
                transform = dst_transform
                width, height = dst_width, dst_height
                geolocation = "embedded_geotiff_reprojected"
            else:
                transform = src.transform
                width, height = src.width, src.height
                geolocation = "embedded_geotiff"
            radiometry = "embedded_values"
            if not np.isfinite(image).any():
                raise ValueError(
                    "The SAR image contains no usable pixels after reprojection to EPSG:4326."
                )
        else:
            width, height = src.width, src.height
            # Plain image uploads have no map coordinates. Anchor their centre
            # on the selected vessel using the Sentinel-1 10 m pixel spacing.
            metres_per_lon_degree = max(1.0, 111_320.0 * math.cos(math.radians(latitude)))
            x_size = 10.0 / metres_per_lon_degree
            y_size = 10.0 / 110_574.0
            transform = from_origin(
                longitude - (src.width * x_size / 2.0),
                latitude + (src.height * y_size / 2.0),
                x_size,
                y_size,
            )
            geolocation = "vessel_position_anchor"

            finite_values = image[np.isfinite(image)]
            # PNG/JPEG SAR products normally contain display intensities rather
            # than calibrated VV backscatter. Preserve already-dB rasters, but
            # map display imagery robustly onto a documented proxy dB range so
            # the existing darkness score is not fed incompatible 0..255 data.
            if float(np.min(finite_values)) < 0.0 and float(np.percentile(finite_values, 98)) <= 20.0:
                radiometry = "provided_db_values"
            else:
                low, high = np.percentile(finite_values, (2.0, 98.0))
                if not np.isfinite(low) or not np.isfinite(high) or high <= low:
                    raise ValueError("The SAR image has insufficient intensity variation for analysis.")
                scaled = np.clip((image - float(low)) / float(high - low), 0.0, 1.0)
                image = (-30.0 + 40.0 * scaled).astype("float32")
                radiometry = "display_intensity_to_db_proxy"

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(
            output_path,
            "w",
            driver="GTiff",
            width=width,
            height=height,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=transform,
            nodata=float("nan"),
            compress="deflate",
        ) as dst:
            dst.write(image, 1)

        west, south, east, north = array_bounds(height, width, transform)

    return {
        "width": int(width),
        "height": int(height),
        "geolocation": geolocation,
        "radiometry": radiometry,
        "band_count": int(band_count),
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
        raise HTTPException(status_code=400, detail="Use a GeoTIFF, TIFF, PNG, or JPEG SAR image.")

    payload = await image.read()
    log.debug("Upload validation: payload size=%d bytes", len(payload))
    if not payload:
        log.debug("Rejected upload: empty payload")
        raise HTTPException(status_code=400, detail="The uploaded SAR image is empty.")
    if len(payload) > _MAX_UPLOAD_BYTES:
        log.debug("Rejected upload: payload exceeds max size (%d > %d)", len(payload), _MAX_UPLOAD_BYTES)
        raise HTTPException(status_code=413, detail="SAR uploads are limited to 50 MB.")

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
            "polarization": "VV",
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
