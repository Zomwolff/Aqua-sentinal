# sar_acquisition.py
"""Synthetic Aperture Radar (SAR) acquisition module for Sentinel‑1.

This module follows the existing worker/main split pattern used in the
`data-ingestion` service. It provides pure functions that can be called from a
periodic asyncio task (e.g. added to ``run_ingestion_worker``). The functions are
side‑effect free except for the Earth Engine export and the final download step.

Key points:
* Earth Engine is initialised from ``GEE_SERVICE_ACCOUNT`` and
  ``GEE_PRIVATE_KEY_PATH`` environment variables.
* AOI geometry is imported from ``shared.spatial.geo.mumbai_aoi_geometry``.
* Export uses ``Export.image.toDrive``; after completion the GeoTIFF is downloaded
  into the shared container mount ``/data/artifacts/sar`` (the ``sar-scene-artifacts`` Docker
  volume).
* Redis message follows the flat‑field JSON convention used for ``ais.clean`` and
  contains ``scene_metadata`` (JSON‑encoded) and ``raster_path`` (container‑visible
  path).
* Geometry area calculations use Earth Engine's native geodesic area (metric
  units).
"""

import os
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple, Optional

import ee
import rasterio
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

from shared.spatial.geo import mumbai_aoi_geometry
from shared.redis_client import get_redis, publish_to_stream

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Earth Engine initialisation
# ---------------------------------------------------------------------------

def init_gee() -> None:
    """Initialise the Earth Engine API using a service‑account JSON.

    ``GEE_SERVICE_ACCOUNT`` points at the service‑account email and
    ``GEE_PRIVATE_KEY_PATH`` points at the private‑key JSON file. This mirrors the
    pattern used elsewhere in the project.
    """
    service_account = os.getenv("GEE_SERVICE_ACCOUNT")
    private_key_path = os.getenv("GEE_PRIVATE_KEY_PATH")

    if not service_account or not private_key_path:
        raise RuntimeError(
            "GEE_SERVICE_ACCOUNT and GEE_PRIVATE_KEY_PATH must be set for SAR acquisition"
        )

    credentials = ee.ServiceAccountCredentials(service_account, private_key_path)
    ee.Initialize(credentials)
    log.info("Earth Engine initialised with service account %s", service_account)


# ---------------------------------------------------------------------------
# Helper: coverage fraction (geodesic area)
# ---------------------------------------------------------------------------

def _coverage_fraction(scene_geom: ee.Geometry, aoi_geom: ee.Geometry) -> float:
    """Return the fraction of the AOI covered by a scene (0‑1)."""
    intersect = scene_geom.intersection(aoi_geom, ee.ErrorMargin(1))
    intersect_area = intersect.area(ee.ErrorMargin(1))
    aoi_area = aoi_geom.area(ee.ErrorMargin(1))
    return float(intersect_area.divide(aoi_area).getInfo())


# ---------------------------------------------------------------------------
# Query Sentinel‑1 collection
# ---------------------------------------------------------------------------

def get_sentinel1_scenes(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Return candidate Sentinel‑1 scenes for the Mumbai AOI.

    Filters applied:
    * platform: ``COPERNICUS/S1_GRD``
    * instrument mode: ``IW``
    * polarisation: ``VV``
    * acquisition time between *start_date* and *end_date* (ISO‑8601).
    """
    aoi = mumbai_aoi_geometry()
    collection = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitPolarisation", "VV"))
    )

    scenes: List[Dict[str, Any]] = []
    for img in collection.toList(collection.size()).getInfo():
        img_obj = ee.Image(img["id"]).rename(["VV"])  # keep only VV band
        scenes.append({
            "id": img["id"],
            "properties": img["properties"],
            "geometry": img_obj.geometry(),
            "image": img_obj,
        })
    log.info("Found %d Sentinel‑1 scenes between %s and %s", len(scenes), start_date, end_date)
    return scenes


# ---------------------------------------------------------------------------
# Ranking – pick the best scene
# ---------------------------------------------------------------------------

def select_best_scene(scenes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Select the scene with highest coverage and most recent acquisition time."""
    aoi = mumbai_aoi_geometry()
    for scene in scenes:
        scene["coverage"] = _coverage_fraction(scene["geometry"], aoi)
        scene["has_vv"] = "VV" in scene["image"].bandNames().getInfo()

    sorted_scenes = sorted(
        scenes,
        key=lambda s: (s.get("coverage", 0), s["properties"].get("system:time_start", 0)),
        reverse=True,
    )
    for cand in sorted_scenes:
        if cand["has_vv"] and cand["coverage"] > 0:
            log.info("Selected scene %s (coverage %.2f%%)", cand["id"], cand["coverage"] * 100)
            return cand
    raise RuntimeError("No suitable Sentinel‑1 scene found for the AOI")


# ---------------------------------------------------------------------------
# Google Drive download helper
# ---------------------------------------------------------------------------

def _build_drive_service(key_path: str):
    """Build a Google Drive API service using the service account credentials."""
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    credentials = service_account.Credentials.from_service_account_file(
        key_path, scopes=scopes
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _download_from_drive(drive_service, file_id: str, dest_path: str) -> None:
    """Download a file from Google Drive by file ID."""
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            log.info("Download progress: %d%%", int(status.progress() * 100))
    fh.close()
    log.info("Downloaded file to %s", dest_path)


def _validate_geotiff(path: str) -> None:
    """Validate that the GeoTIFF is readable and has expected structure."""
    with rasterio.open(path) as ds:
        if ds.width == 0 or ds.height == 0:
            raise ValueError(f"GeoTIFF has zero dimensions: {ds.width}x{ds.height}")
        if ds.count == 0:
            raise ValueError("GeoTIFF has no bands")
        log.info("GeoTIFF validation passed: %dx%d, %d band(s), CRS=%s",
                 ds.width, ds.height, ds.count, ds.crs)
        # Read a small sample to verify data integrity
        _ = ds.read(1, window=rasterio.windows.Window(0, 0, min(10, ds.width), min(10, ds.height)))


# ---------------------------------------------------------------------------
# Export and download helpers
# ---------------------------------------------------------------------------

def _download_exported_file(task: Any, folder: str, file_prefix: str) -> str:
    """Poll the EE export task until completion, then download from Google Drive.

    Waits for the Earth Engine export task to reach COMPLETED state, extracts
    the Google Drive file ID from the task status, downloads the GeoTIFF via
    the Drive API, validates it with rasterio, and returns the local path.
    """
    max_wait = 600  # seconds
    elapsed = 0
    while elapsed < max_wait:
        status = task.status()
        state = status.get("state")
        if state == "COMPLETED":
            break
        if state in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"EE export task failed with state {state}")
        time.sleep(5)
        elapsed += 5
    else:
        raise TimeoutError(f"EE export task did not complete within {max_wait}s")

    # Extract Google Drive file ID from task status
    # EE task status for Drive exports includes 'destination_uris' with the file ID
    destination_uris = status.get("destination_uris", [])
    if not destination_uris:
        # Fallback: try to find the file by name in the export folder
        log.warning("No destination_uris in task status, attempting to find file by name")
        file_id = _find_file_in_drive_folder(folder, file_prefix)
    else:
        # destination_uris format: ["https://drive.google.com/file/d/<FILE_ID>/view"]
        uri = destination_uris[0]
        if "/file/d/" in uri:
            file_id = uri.split("/file/d/")[1].split("/")[0]
        else:
            raise RuntimeError(f"Unexpected destination_uri format: {uri}")

    log.info("EE export completed, downloading file ID: %s", file_id)

    # Build Drive service and download
    key_path = os.getenv("GEE_PRIVATE_KEY_PATH")
    if not key_path:
        raise RuntimeError("GEE_PRIVATE_KEY_PATH not set for Drive download")
    drive_service = _build_drive_service(key_path)

    dest_dir = "/data/artifacts/sar"
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, f"{file_prefix}.tif")

    _download_from_drive(drive_service, file_id, dest_path)

    # Validate the downloaded GeoTIFF
    _validate_geotiff(dest_path)

    log.info("SAR raster downloaded and validated: %s", dest_path)
    return dest_path


def _find_file_in_drive_folder(folder_name: str, file_prefix: str) -> str:
    """Fallback: search for the exported file in the Drive folder by name."""
    key_path = os.getenv("GEE_PRIVATE_KEY_PATH")
    if not key_path:
        raise RuntimeError("GEE_PRIVATE_KEY_PATH not set for Drive search")
    drive_service = _build_drive_service(key_path)

    # Find folder ID
    folder_query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    folder_results = drive_service.files().list(q=folder_query, fields="files(id)").execute()
    folders = folder_results.get("files", [])
    if not folders:
        raise RuntimeError(f"Drive folder '{folder_name}' not found")
    folder_id = folders[0]["id"]

    # Find file in folder
    file_query = f"name='{file_prefix}.tif' and '{folder_id}' in parents and trashed=false"
    file_results = drive_service.files().list(q=file_query, fields="files(id)").execute()
    files = file_results.get("files", [])
    if not files:
        raise RuntimeError(f"File '{file_prefix}.tif' not found in Drive folder '{folder_name}'")
    return files[0]["id"]

# ---------------------------------------------------------------------------
# Export a scene to the shared SAR volume
# ---------------------------------------------------------------------------

def export_scene_metadata(
    scene: Dict[str, Any],
    *,
    inject_synthetic: Optional[bool] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Export the scene via ``Export.image.toDrive`` and return local raster path.

    ``inject_synthetic`` is an explicit opt-in override for synthetic demo
    injection (see ``synthetic_injection``); when omitted the
    ``INJECT_SYNTHETIC`` environment configuration decides. Synthetic injection
    is disabled by default and never overwrites the original raster.
    """
    scene_id = scene["id"].replace("/", "_")
    image = scene["image"]

    export_folder = "sar_export_tmp"
    file_prefix = scene_id
    task = ee.batch.Export.image.toDrive(
        image=image.select("VV"),
        description=f"export_{scene_id}",
        folder=export_folder,
        fileNamePrefix=file_prefix,
        scale=10,
        region=image.geometry(),
        fileFormat="GeoTIFF",
        maxPixels=1e10,
    )
    task.start()

    raster_path = _download_exported_file(task, export_folder, file_prefix)

    from app.synthetic_injection import inject_geotiff_if_enabled

    raster_path, synthetic_meta = inject_geotiff_if_enabled(
        raster_path,
        scene_id,
        explicit=inject_synthetic,
    )

    metadata = {
        "scene_id": scene["id"],
        "acquisition_time": datetime.utcfromtimestamp(
            scene["properties"]["acquisition_time"] / 1000
        ).isoformat()
        + "Z",
        "orbit": scene["properties"].get("orbit"),
        "polarization": scene["properties"].get("polarization"),
        "resolution": scene["properties"].get("resolution"),
    }
    metadata.update(synthetic_meta)
    return raster_path, metadata

# ---------------------------------------------------------------------------
# Publish to Redis
# ---------------------------------------------------------------------------

def publish_sar_scene(raster_path: str, metadata: Dict[str, Any]) -> None:
    """Publish a flat‑field JSON message to the ``sar.clean`` Redis stream."""
    async def _publish():
        redis = await get_redis()
        await publish_to_stream(redis, "sar.clean", {
            "scene_metadata": json.dumps(metadata),
            "raster_path": raster_path,
        })
        log.info("Published SAR scene %s", metadata.get("scene_id"))

    import asyncio
    loop = asyncio.get_event_loop()
    if loop.is_running():
        asyncio.create_task(_publish())
    else:
        loop.run_until_complete(_publish())

# ---------------------------------------------------------------------------
# High‑level entry point used by the periodic worker
# ---------------------------------------------------------------------------

def run_sar_acquisition(
    start_date: str,
    end_date: str,
    *,
    inject_synthetic: Optional[bool] = None,
) -> None:
    """Execute the full acquisition pipeline for a date range.

    ``inject_synthetic`` is an explicit opt-in override; when omitted the
    ``INJECT_SYNTHETIC`` environment variable decides (disabled by default).
    """
    init_gee()
    scenes = get_sentinel1_scenes(start_date, end_date)
    best = select_best_scene(scenes)
    raster_path, meta = export_scene_metadata(best, inject_synthetic=inject_synthetic)
    publish_sar_scene(raster_path, meta)

__all__ = [
    "init_gee",
    "get_sentinel1_scenes",
    "select_best_scene",
    "export_scene_metadata",
    "publish_sar_scene",
    "run_sar_acquisition",
]
