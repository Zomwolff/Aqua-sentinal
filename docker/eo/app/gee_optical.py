"""Sentinel-2 acquisition for the `eo` service.

Deliberately mirrors sar-cfar/acquisition/sar_acquisition.py's structure
(same GEE auth pattern, same Export.image.toDrive + Drive-download flow) so
anyone who already understands the SAR acquisition path can read this one
for free. The two differences that matter:

  1. Collection is COPERNICUS/S2_SR_HARMONIZED, not S1_GRD — optical, not
     radar — and it's filtered by CLOUDY_PIXEL_PERCENTAGE, since cloud cover
     is the whole reason optical is secondary/non-mandatory evidence here.
  2. Bands are exported in `train_cnn.FEATURE_NAMES` order (B2, B3, B4, B5,
     B6, B7, B8, B8A, B11, B12) — 20 m bands are already resampled to 10 m by
     GEE's `.resample()` before stacking, so the exported GeoTIFF has ten
     bands at one common resolution, matching what sentinel2_interface.py
     expects (it will refuse a raster with the wrong band count).

If no cloud-free scene exists in the search window, `find_scene` returns
None — this is a normal, silent outcome (see the project's own design note:
"don't make Sentinel-2 mandatory because optical imagery can be blocked by
clouds"), not an error.
"""
from __future__ import annotations

import io
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import ee
import rasterio
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

log = logging.getLogger(__name__)

# Must match optical imagery/train_cnn.py's FEATURE_NAMES order exactly —
# sentinel2_interface.get_data() rejects a raster whose band count doesn't
# match len(FEATURE_NAMES), and reads bands positionally in this order.
BAND_ORDER = ["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12"]
TEN_M_BANDS = {"B2", "B3", "B4", "B8"}  # rest are natively 20 m, resampled up

DEFAULT_CLOUD_PCT_MAX = float(os.environ.get("EO_CLOUD_PCT_MAX", "20"))
DEFAULT_SEARCH_WINDOW_DAYS = int(os.environ.get("EO_SEARCH_WINDOW_DAYS", "5"))
EXPORT_SCALE_M = int(os.environ.get("EO_EXPORT_SCALE_M", "10"))


def init_gee() -> None:
    """Initialise Earth Engine — identical pattern/env vars to sar_acquisition.py
    (both run inside the same GEE service account credentials)."""
    account = os.getenv("GEE_SERVICE_ACCOUNT")
    key_path = os.getenv("GEE_PRIVATE_KEY_PATH")
    if not account or not key_path:
        raise RuntimeError("GEE_SERVICE_ACCOUNT and GEE_PRIVATE_KEY_PATH must be set for EO acquisition")
    credentials = ee.ServiceAccountCredentials(account, key_path)
    ee.Initialize(credentials)
    log.info("Earth Engine initialised for eo service (account=%s)", account)


def find_scene(
    lat: float,
    lon: float,
    acquisition_time: datetime,
    *,
    window_days: int = DEFAULT_SEARCH_WINDOW_DAYS,
    cloud_pct_max: float = DEFAULT_CLOUD_PCT_MAX,
) -> Optional[Dict[str, Any]]:
    """Find the least-cloudy Sentinel-2 scene near a SAR candidate's location/time.

    Searches +/- `window_days` around the SAR acquisition_time (SAR and
    optical passes are rarely simultaneous) within a small buffer around the
    candidate point (reuses the same AOI helper as SAR acquisition, clipped
    to a point buffer rather than the full AOI, since we only need coverage
    of this one candidate, not the whole harbour). Returns None if nothing
    within `cloud_pct_max` cloud cover exists — a normal outcome.
    """
    point = ee.Geometry.Point([lon, lat]).buffer(5000)  # 5 km buffer around the candidate
    start = (acquisition_time - timedelta(days=window_days)).strftime("%Y-%m-%d")
    end = (acquisition_time + timedelta(days=window_days)).strftime("%Y-%m-%d")

    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(point)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", cloud_pct_max))
        .sort("CLOUDY_PIXEL_PERCENTAGE")  # least cloudy first
    )
    size = collection.size().getInfo()
    if size == 0:
        log.info(
            "No Sentinel-2 scene under %.0f%% cloud cover for (%.4f, %.4f) between %s and %s",
            cloud_pct_max, lat, lon, start, end,
        )
        return None

    img = ee.Image(collection.first())
    info = img.getInfo()
    scene_id = info["id"]
    cloud_pct = info["properties"].get("CLOUDY_PIXEL_PERCENTAGE")
    log.info("Selected Sentinel-2 scene %s (cloud=%.1f%%)", scene_id, cloud_pct or -1)
    return {"id": scene_id, "image": img, "geometry": point, "cloud_pct": cloud_pct}


def _build_stack(img: "ee.Image") -> "ee.Image":
    """Resample 20 m bands to 10 m and stack in BAND_ORDER."""
    bands = []
    for name in BAND_ORDER:
        band = img.select(name)
        if name not in TEN_M_BANDS:
            band = band.resample("bilinear").reproject(crs=img.select("B2").projection(), scale=EXPORT_SCALE_M)
        bands.append(band)
    return ee.Image.cat(bands).rename(BAND_ORDER)


def _build_drive_service(key_path: str):
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    credentials = service_account.Credentials.from_service_account_file(key_path, scopes=scopes)
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _download_from_drive(drive_service, file_id: str, dest_path: str) -> None:
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.close()


def _validate_geotiff(path: str, expected_bands: int) -> None:
    with rasterio.open(path) as ds:
        if ds.width == 0 or ds.height == 0:
            raise ValueError(f"EO GeoTIFF has zero dimensions: {ds.width}x{ds.height}")
        if ds.count != expected_bands:
            raise ValueError(f"EO GeoTIFF has {ds.count} bands, expected {expected_bands}")


def export_and_download(scene: Dict[str, Any], candidate_id: str) -> str:
    """Export the 10-band stack to Drive, wait for completion, download it.

    Blocking (polls task.status() with a sleep, same as sar_acquisition.py) —
    call this from a background worker, never from a request handler.
    Returns the local GeoTIFF path.
    """
    stack = _build_stack(scene["image"])
    file_prefix = f"eo_{candidate_id}"
    task = ee.batch.Export.image.toDrive(
        image=stack,
        description=file_prefix,
        folder="aqua_sentinel_eo",
        fileNamePrefix=file_prefix,
        region=scene["geometry"],
        scale=EXPORT_SCALE_M,
        maxPixels=1e9,
    )
    task.start()

    max_wait = 600
    elapsed = 0
    while elapsed < max_wait:
        status = task.status()
        state = status.get("state")
        if state == "COMPLETED":
            break
        if state in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"EO export task failed with state {state}")
        time.sleep(5)
        elapsed += 5
    else:
        raise TimeoutError(f"EO export task did not complete within {max_wait}s")

    destination_uris = status.get("destination_uris", [])
    if not destination_uris:
        raise RuntimeError("EO export completed but no destination_uris in task status")
    uri = destination_uris[0]
    if "/file/d/" not in uri:
        raise RuntimeError(f"Unexpected destination_uri format: {uri}")
    file_id = uri.split("/file/d/")[1].split("/")[0]

    key_path = os.getenv("GEE_PRIVATE_KEY_PATH")
    drive_service = _build_drive_service(key_path)
    dest_dir = "/data/artifacts/eo"
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, f"{file_prefix}.tif")
    _download_from_drive(drive_service, file_id, dest_path)
    _validate_geotiff(dest_path, expected_bands=len(BAND_ORDER))
    return dest_path
