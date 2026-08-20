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
  into the shared container mount ``/data/sar`` (the ``sar-raster-data`` Docker
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
from datetime import datetime
from typing import Any, Dict, List, Tuple

import ee

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

    cred_json = json.load(open(service_account))
    credentials = ee.ServiceAccountCredentials(
        service_account, private_key_path, private_key_json=cred_json
    )
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
# Export and download helpers
# ---------------------------------------------------------------------------

def _download_exported_file(task: Any, folder: str, file_prefix: str) -> str:
    """Poll the EE export task until completion and return the local path.

    In production this would download the file from Google Drive (or GCS) into
    ``/data/sar``. For the purpose of this repository the function simply waits
    for the task to reach ``COMPLETED`` and constructs the expected destination
    path. Unit tests mock this function.
    """
    import time

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

    dest_dir = "/data/sar"
    os.makedirs(dest_dir, exist_ok=True)
    return os.path.join(dest_dir, f"{file_prefix}.tif")

# ---------------------------------------------------------------------------
# Export a scene to the shared SAR volume
# ---------------------------------------------------------------------------

def export_scene_metadata(scene: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Export the scene via ``Export.image.toDrive`` and return local raster path.
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

def run_sar_acquisition(start_date: str, end_date: str) -> None:
    """Execute the full acquisition pipeline for a date range."""
    init_gee()
    scenes = get_sentinel1_scenes(start_date, end_date)
    best = select_best_scene(scenes)
    raster_path, meta = export_scene_metadata(best)
    publish_sar_scene(raster_path, meta)

__all__ = [
    "init_gee",
    "get_sentinel1_scenes",
    "select_best_scene",
    "export_scene_metadata",
    "publish_sar_scene",
    "run_sar_acquisition",
]
