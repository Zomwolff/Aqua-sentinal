import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
import ee

from shared.db import get_pool
from app.sar_acquisition import (
    init_gee,
    get_sentinel1_scenes,
    select_best_scene,
    export_scene_metadata,
    publish_sar_scene,
)

log = logging.getLogger("data-ingestion")


def _env_flag(name: str, default: str = "false") -> bool:
    """Parse a boolean environment flag. Defaults to OFF (research integrity)."""
    return str(os.environ.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


async def process_task(task_id: int, mmsi: str, lat: float, lon: float, pool):
    log.info(f"Processing SAR tasking request {task_id} for MMSI {mmsi} at ({lat}, {lon})")

    # Synthetic slick injection is OPT-IN (env INJECT_SYNTHETIC, default OFF).
    # Real operational taskings must never be contaminated with demo evidence.
    inject_synthetic = _env_flag("INJECT_SYNTHETIC")
    # Mock-scene fallback on GEE failure is OPT-IN as well (default OFF):
    # without a real scene the tasking is marked 'failed' instead of feeding
    # fabricated data into the spill pipeline.
    allow_mock_fallback = _env_flag("SAR_ALLOW_MOCK_FALLBACK")

    # Run GEE fetching in a separate thread since it blocks and can take a while
    def _fetch_sar():
        try:
            init_gee()
            # Bounding box of ~20km around the vessel
            point = ee.Geometry.Point([lon, lat])
            aoi = point.buffer(10000).bounds()
            
            # Look back 5 years to guarantee we find a Sentinel-1 scene over this coordinate
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=1825)
            
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")
            
            scenes = get_sentinel1_scenes(start_str, end_str, aoi_geom=aoi)
            best = select_best_scene(scenes, aoi_geom=aoi)
            
            # inject_synthetic=None -> resolved inside export via resolve_synthetic_enabled
            raster_path, meta = export_scene_metadata(best, aoi=aoi, inject_synthetic=inject_synthetic)
            
        except Exception as e:
            if not allow_mock_fallback:
                log.error(
                    f"GEE acquisition failed for tasking {task_id} and mock fallback is "
                    f"disabled (SAR_ALLOW_MOCK_FALLBACK=false): {e}"
                )
                raise
            log.warning(f"GEE failed ({e}), generating a mock GeoTIFF for SAR pipeline demonstration")
            import numpy as np
            import rasterio
            from rasterio.transform import from_origin
            from app.synthetic_injection import inject_geotiff_if_enabled
            import os
            
            # Create a mock 1000x1000 GeoTIFF (simulating SAR backscatter -10 to -20 dB)
            os.makedirs("/data/artifacts/sar", exist_ok=True)
            raster_path = f"/data/artifacts/sar/mock_scene_{task_id}.tif"
            
            # Generate random backscatter noise
            data = np.random.normal(loc=-15.0, scale=3.0, size=(1000, 1000)).astype(np.float64)
            transform = from_origin(lon - 0.1, lat + 0.1, 0.0002, 0.0002)
            
            with rasterio.open(
                raster_path, 'w', driver='GTiff',
                height=data.shape[0], width=data.shape[1],
                count=1, dtype=data.dtype,
                crs='EPSG:4326', transform=transform
            ) as dst:
                dst.write(data, 1)
                
            raster_path, synthetic_meta = inject_geotiff_if_enabled(
                raster_path,
                scene_id=f"mock_scene_{task_id}",
                explicit=True,
                length_px=300,
                width_px=40,
                angle_deg=45.0,
                darkness_db=-8.0
            )
            
            meta = {
                "scene_id": f"mock_scene_{task_id}",
                "acquisition_time": datetime.utcnow().isoformat() + "Z",
                "orbit": "mock",
                "polarization": "VV",
                "resolution": 10.0,
                "is_synthetic": True,
            }
            meta.update(synthetic_meta)
            
        custom_scene_id = f"SCENE_TASKING_{task_id}"
        meta["scene_id"] = custom_scene_id
        
        return raster_path, meta, custom_scene_id

    try:
        raster_path, meta, scene_id = await asyncio.to_thread(_fetch_sar)
        
        # The scene may be acquired before its PNG previews have been written.
        # Keep this task out of the polling queue, but do not report it as
        # fulfilled until sar-spill-intelligence has persisted every artifact.
        await pool.execute(
            "UPDATE satellite_tasking_requests SET status = 'processing', scene_id = $1 WHERE id = $2",
            scene_id, task_id
        )

        # Publish from the main asyncio event loop, not the background thread.
        publish_sar_scene(raster_path, meta)
        log.info(f"SAR tasking {task_id} acquired scene {scene_id}; awaiting artifact generation")
    except Exception as e:
        log.error(f"Failed to process SAR tasking {task_id}: {e}")
        await pool.execute(
            "UPDATE satellite_tasking_requests SET status = 'failed', completed_at = NOW() WHERE id = $1",
            task_id
        )


async def dynamic_sar_worker():
    print("====== DYNAMIC SAR WORKER STARTED ======", flush=True)
    log.info("Dynamic SAR Tasking worker started.")
    try:
        pool = await get_pool()
        
        while True:
            try:
                # Poll for pending tasking requests
                rows = await pool.fetch(
                    "SELECT id, mmsi FROM satellite_tasking_requests WHERE status = 'pending' ORDER BY requested_at ASC"
                )
                if rows:
                    print(f"====== FOUND {len(rows)} PENDING TASKING REQUESTS ======", flush=True)
                
                for row in rows:
                    task_id = row["id"]
                    mmsi = row["mmsi"]
                    
                    # Fetch vessel coordinates
                    vessel = await pool.fetchrow(
                        "SELECT last_lat, last_lon FROM vessels WHERE mmsi = $1", mmsi
                    )
                    
                    if vessel and vessel["last_lat"] and vessel["last_lon"]:
                        await process_task(task_id, mmsi, vessel["last_lat"], vessel["last_lon"], pool)
                    else:
                        log.warning(f"Cannot fulfill tasking {task_id} for MMSI {mmsi} due to missing coordinates")
                        await pool.execute(
                            "UPDATE satellite_tasking_requests SET status = 'failed' WHERE id = $1",
                            task_id
                        )
            except Exception as e:
                log.error(f"Dynamic SAR worker polling error: {e}")
                print(f"Dynamic SAR worker polling error: {e}", flush=True)
                
            await asyncio.sleep(5)
    except Exception as e:
        print(f"====== DYNAMIC SAR WORKER FATAL ERROR: {e} ======", flush=True)
        log.exception("Fatal error in dynamic_sar_worker")
