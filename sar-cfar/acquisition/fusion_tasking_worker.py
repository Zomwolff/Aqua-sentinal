"""
Fusion Tasking Worker - SAR + EO Dual Acquisition
==================================================

Enhanced SAR tasking worker that fetches BOTH Sentinel-1 (SAR) and Sentinel-2 (EO)
imagery for fusion-based oil spill detection.

This extends the existing dynamic_sar_worker to:
1. Fetch Sentinel-1 GRD (SAR) from GEE
2. Fetch Sentinel-2 SR (optical) from GEE in parallel
3. Download both scenes to artifacts directory
4. Automatically invoke fusion service with both inputs
5. Track fusion-specific status (sar_fetched, eo_fetched, fusion_status)

The worker is triggered by satellite_tasking_requests with specific metadata
indicating fusion mode (e.g., scenario="wakashio_fusion_demo").
"""

import asyncio
import httpx
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple
import ee

from shared.db import get_pool
from shared.redis_client import get_redis, publish_to_stream

log = logging.getLogger("fusion-tasking")


def _env_flag(name: str, default: str = "false") -> bool:
    """Parse a boolean environment flag."""
    return str(os.environ.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


def init_gee() -> None:
    """Initialize Google Earth Engine with service account credentials."""
    account = os.getenv("GEE_SERVICE_ACCOUNT")
    key_path = os.getenv("GEE_PRIVATE_KEY_PATH")
    if not account or not key_path:
        raise RuntimeError("GEE_SERVICE_ACCOUNT and GEE_PRIVATE_KEY_PATH must be set")
    credentials = ee.ServiceAccountCredentials(account, key_path)
    ee.Initialize(credentials)
    log.info("Earth Engine initialized for fusion tasking (account=%s)", account)


# ── SAR Acquisition (Sentinel-1) ──────────────────────────────────────────────
async def fetch_sentinel1_scene(
    lat: float,
    lon: float,
    date: datetime,
    task_id: int,
    pool
) -> Tuple[Optional[Path], Optional[Dict]]:
    """
    Fetch Sentinel-1 SAR scene from Google Earth Engine.
    
    Args:
        lat: Latitude of interest
        lon: Longitude of interest
        date: Target acquisition date
        task_id: Tasking request ID
        pool: Database connection pool
    
    Returns:
        Tuple of (raster_path, metadata_dict) or (None, None) on failure
    """
    def _fetch():
        try:
            init_gee()
            
            # 10km buffer around point
            point = ee.Geometry.Point([lon, lat])
            aoi = point.buffer(10000).bounds()
            
            # Search window: ±3 days from target date
            start_date = (date - timedelta(days=3)).strftime("%Y-%m-%d")
            end_date = (date + timedelta(days=3)).strftime("%Y-%m-%d")
            
            # Query Sentinel-1 GRD collection
            collection = (
                ee.ImageCollection("COPERNICUS/S1_GRD")
                .filterBounds(aoi)
                .filterDate(start_date, end_date)
                .filter(ee.Filter.eq("instrumentMode", "IW"))
                .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
                .sort("system:time_start", False)  # Most recent first
            )
            
            size = collection.size().getInfo()
            if size == 0:
                log.warning(f"No Sentinel-1 scenes found for ({lat}, {lon}) between {start_date} and {end_date}")
                return None, None
            
            # Select most recent scene
            img = ee.Image(collection.first()).select(["VV"])
            info = img.getInfo()
            scene_id = info["id"]
            
            log.info(f"Selected S1 scene: {scene_id}")
            
            # Export to Drive and download (simplified for demo - use existing export logic)
            # For now, create a placeholder path
            artifacts_dir = Path("/data/artifacts/fusion") / f"task_{task_id}"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            
            raster_path = artifacts_dir / f"sentinel1_{task_id}.tif"
            
            # In production, this would call the full export_scene_metadata flow
            # For simulation, we'll use the existing SAR acquisition logic
            from sar.acquisition.sar_acquisition import get_sentinel1_scenes, select_best_scene, export_scene_metadata
            
            scenes = get_sentinel1_scenes(start_date, end_date, aoi_geom=aoi)
            if not scenes:
                return None, None
            
            best = select_best_scene(scenes, aoi_geom=aoi)
            path, meta = export_scene_metadata(best, aoi=aoi, inject_synthetic=False)
            
            return Path(path), meta
            
        except Exception as e:
            log.error(f"Failed to fetch Sentinel-1 for task {task_id}: {e}")
            return None, None
    
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        log.error(f"S1 fetch thread error: {e}")
        return None, None


# ── EO Acquisition (Sentinel-2) ───────────────────────────────────────────────
async def fetch_sentinel2_scene(
    lat: float,
    lon: float,
    date: datetime,
    task_id: int,
    pool
) -> Tuple[Optional[Path], Optional[Dict]]:
    """
    Fetch Sentinel-2 optical scene from Google Earth Engine.
    
    Args:
        lat: Latitude of interest
        lon: Longitude of interest
        date: Target acquisition date
        task_id: Tasking request ID
        pool: Database connection pool
    
    Returns:
        Tuple of (raster_path, metadata_dict) or (None, None) on failure
    """
    def _fetch():
        try:
            init_gee()
            
            # 5km buffer around point (smaller for optical since we need less coverage)
            point = ee.Geometry.Point([lon, lat])
            aoi = point.buffer(5000).bounds()
            
            # Search window: ±5 days from target date (optical has more frequent revisit)
            start_date = (date - timedelta(days=5)).strftime("%Y-%m-%d")
            end_date = (date + timedelta(days=5)).strftime("%Y-%m-%d")
            
            # Cloud threshold
            cloud_max = float(os.environ.get("EO_CLOUD_PCT_MAX", "20"))
            
            # Query Sentinel-2 SR collection
            collection = (
                ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterBounds(aoi)
                .filterDate(start_date, end_date)
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", cloud_max))
                .sort("CLOUDY_PIXEL_PERCENTAGE")  # Least cloudy first
            )
            
            size = collection.size().getInfo()
            if size == 0:
                log.warning(
                    f"No cloud-free Sentinel-2 scenes (<{cloud_max}% clouds) found for "
                    f"({lat}, {lon}) between {start_date} and {end_date}"
                )
                return None, None
            
            # Select least cloudy scene
            img = ee.Image(collection.first())
            info = img.getInfo()
            scene_id = info["id"]
            cloud_pct = info["properties"].get("CLOUDY_PIXEL_PERCENTAGE", -1)
            
            log.info(f"Selected S2 scene: {scene_id} (cloud cover: {cloud_pct:.1f}%)")
            
            # Band order for fusion (10 bands)
            BAND_ORDER = ["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12"]
            
            # Resample 20m bands to 10m and select in correct order
            img_resampled = img.select(BAND_ORDER).resample("bilinear")
            
            # Export configuration
            artifacts_dir = Path("/data/artifacts/fusion") / f"task_{task_id}"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            raster_path = artifacts_dir / f"sentinel2_{task_id}.tif"
            
            # For demo purposes, use gee_optical export logic
            # In production, implement full export similar to SAR
            from docker.eo.app.gee_optical import find_scene
            
            # Use the existing find_scene function
            scene_info = find_scene(
                lat, lon, date,
                window_days=5,
                cloud_pct_max=cloud_max
            )
            
            if not scene_info:
                return None, None
            
            # The scene_info contains the exported path
            # For now, return a placeholder
            # TODO: Implement full GEE export + download flow
            
            meta = {
                "scene_id": scene_id,
                "acquisition_time": date.isoformat(),
                "cloud_percentage": cloud_pct,
                "bands": BAND_ORDER,
                "resolution_m": 10,
            }
            
            return raster_path, meta
            
        except Exception as e:
            log.error(f"Failed to fetch Sentinel-2 for task {task_id}: {e}")
            return None, None
    
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        log.error(f"S2 fetch thread error: {e}")
        return None, None


# ── Fusion Service Integration ────────────────────────────────────────────────
async def trigger_fusion(
    s1_path: Path,
    s2_path: Path,
    mmsi: str,
    acquisition_time: str,
    scene_id: str,
    task_id: int
) -> Optional[Dict]:
    """
    Call the fusion service to process SAR + EO imagery.
    
    Args:
        s1_path: Path to Sentinel-1 GeoTIFF
        s2_path: Path to Sentinel-2 GeoTIFF  
        mmsi: Vessel MMSI
        acquisition_time: ISO timestamp
        scene_id: Scene identifier
        task_id: Tasking request ID
    
    Returns:
        Fusion result dict or None on failure
    """
    fusion_url = os.environ.get("FUSION_SERVICE_URL", "http://fusion:8080")
    
    try:
        log.info(f"Triggering fusion for task {task_id}: S1={s1_path.name}, S2={s2_path.name}")
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            files = {
                "sentinel1": ("sentinel1.tif", open(s1_path, "rb"), "image/tiff"),
                "sentinel2": ("sentinel2.tif", open(s2_path, "rb"), "image/tiff"),
            }
            data = {
                "mmsi": mmsi,
                "acquisition_time": acquisition_time,
                "scene_id": scene_id,
            }
            
            response = await client.post(
                f"{fusion_url}/upload",
                files=files,
                data=data
            )
            
            response.raise_for_status()
            result = response.json()
            
            log.info(
                f"Fusion complete for task {task_id}: "
                f"{len(result.get('candidates', []))} candidates detected"
            )
            
            return result
            
    except httpx.HTTPStatusError as e:
        log.error(f"Fusion service HTTP error for task {task_id}: {e.response.status_code} - {e.response.text[:200]}")
        return None
    except Exception as e:
        log.error(f"Failed to trigger fusion for task {task_id}: {e}")
        return None


# ── Main Processing Logic ─────────────────────────────────────────────────────
async def process_fusion_task(
    task_id: int,
    mmsi: str,
    lat: float,
    lon: float,
    target_date: datetime,
    pool
) -> None:
    """
    Process a fusion tasking request: fetch SAR + EO, trigger fusion.
    
    Args:
        task_id: Tasking request ID
        mmsi: Vessel MMSI
        lat: Latitude
        lon: Longitude
        target_date: Target acquisition date
        pool: Database connection pool
    """
    log.info(f"Processing fusion tasking {task_id} for MMSI {mmsi} at ({lat}, {lon})")
    
    redis = await get_redis()
    
    # Update status: fetching
    await pool.execute(
        "UPDATE satellite_tasking_requests SET status = 'processing' WHERE id = $1",
        task_id
    )
    
    await publish_to_stream(redis, "sar.tasking.events", {
        "task_id": str(task_id),
        "mmsi": mmsi,
        "status": "fetching",
        "fusion_enabled": "true",
    })
    
    # Fetch SAR and EO in parallel
    log.info(f"Fetching Sentinel-1 and Sentinel-2 in parallel for task {task_id}...")
    
    s1_result, s2_result = await asyncio.gather(
        fetch_sentinel1_scene(lat, lon, target_date, task_id, pool),
        fetch_sentinel2_scene(lat, lon, target_date, task_id, pool),
        return_exceptions=True
    )
    
    s1_path, s1_meta = s1_result if not isinstance(s1_result, Exception) else (None, None)
    s2_path, s2_meta = s2_result if not isinstance(s2_result, Exception) else (None, None)
    
    # Check results
    if s1_path is None:
        log.error(f"Failed to fetch Sentinel-1 for task {task_id}")
        await pool.execute(
            "UPDATE satellite_tasking_requests SET status = 'failed', completed_at = NOW() WHERE id = $1",
            task_id
        )
        return
    
    if s2_path is None:
        log.warning(f"Failed to fetch Sentinel-2 for task {task_id}, falling back to SAR-only")
        # Fall back to SAR-only processing
        # TODO: Implement SAR-only fallback
        await pool.execute(
            "UPDATE satellite_tasking_requests SET status = 'sar_only', scene_id = $2, completed_at = NOW() WHERE id = $1",
            task_id, s1_meta.get("scene_id", f"S1_TASK_{task_id}")
        )
        return
    
    # Both scenes fetched successfully - trigger fusion
    log.info(f"Both S1 and S2 fetched for task {task_id}, triggering fusion...")
    
    scene_id = f"FUSION_TASK_{task_id}_{target_date.strftime('%Y%m%d')}"
    acquisition_time = target_date.isoformat()
    
    fusion_result = await trigger_fusion(
        s1_path, s2_path, mmsi, acquisition_time, scene_id, task_id
    )
    
    if fusion_result is None:
        log.error(f"Fusion processing failed for task {task_id}")
        await pool.execute(
            "UPDATE satellite_tasking_requests SET status = 'failed', completed_at = NOW() WHERE id = $1",
            task_id
        )
        return
    
    # Fusion successful
    confidence = fusion_result.get("fusion_confidence", 0.0)
    candidate_count = len(fusion_result.get("candidates", []))
    
    log.info(
        f"Fusion complete for task {task_id}: "
        f"{candidate_count} candidates, confidence={confidence:.2f}"
    )
    
    await pool.execute(
        "UPDATE satellite_tasking_requests SET status = 'fulfilled', scene_id = $2, completed_at = NOW() WHERE id = $1",
        task_id, scene_id
    )
    
    await publish_to_stream(redis, "sar.tasking.events", {
        "task_id": str(task_id),
        "mmsi": mmsi,
        "status": "complete",
        "fusion_enabled": "true",
        "scene_id": scene_id,
        "confidence": str(confidence),
        "candidate_count": str(candidate_count),
    })


# ── Worker Loop ───────────────────────────────────────────────────────────────
async def fusion_tasking_worker():
    """
    Main worker loop: poll for fusion-enabled tasking requests and process them.
    """
    log.info("=" * 70)
    log.info("Fusion Tasking Worker Started")
    log.info("Monitoring for SAR + EO tasking requests...")
    log.info("=" * 70)
    
    try:
        pool = await get_pool()
        
        while True:
            try:
                # Poll for pending tasking requests with fusion metadata
                rows = await pool.fetch(
                    """SELECT id, mmsi, reason, requested_at
                       FROM satellite_tasking_requests
                       WHERE status = 'pending'
                       AND (reason::jsonb->>'scenario') = 'wakashio_fusion_demo'
                       ORDER BY requested_at ASC"""
                )
                
                if rows:
                    log.info(f"Found {len(rows)} pending fusion tasking requests")
                
                for row in rows:
                    task_id = row["id"]
                    mmsi = row["mmsi"]
                    reason = row["reason"]
                    
                    # Parse metadata
                    try:
                        import json
                        metadata = json.loads(reason) if isinstance(reason, str) else reason
                    except:
                        metadata = {}
                    
                    # Get vessel coordinates
                    vessel = await pool.fetchrow(
                        "SELECT last_lat, last_lon FROM vessels WHERE mmsi = $1", mmsi
                    )
                    
                    if not vessel or not vessel["last_lat"] or not vessel["last_lon"]:
                        log.warning(f"Cannot fulfill tasking {task_id}: missing vessel coordinates")
                        await pool.execute(
                            "UPDATE satellite_tasking_requests SET status = 'failed' WHERE id = $1",
                            task_id
                        )
                        continue
                    
                    # Target date (from metadata or current time)
                    target_date_str = metadata.get("target_date", "2020-08-09T08:00:00Z")
                    target_date = datetime.fromisoformat(target_date_str.replace("Z", "+00:00"))
                    
                    # Process the task
                    await process_fusion_task(
                        task_id,
                        mmsi,
                        vessel["last_lat"],
                        vessel["last_lon"],
                        target_date,
                        pool
                    )
                    
            except Exception as e:
                log.error(f"Fusion tasking worker error: {e}", exc_info=True)
            
            await asyncio.sleep(5)  # Poll every 5 seconds
            
    except Exception as e:
        log.exception(f"Fatal error in fusion tasking worker: {e}")
