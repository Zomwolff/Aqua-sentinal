import logging
import asyncio
import httpx
import time
from datetime import datetime
from typing import Optional, Any

log = logging.getLogger(__name__)

async def find_origin_probability(
    lat: float, lon: float, area_m2: float, detection_time: datetime,
) -> Optional[Any]:
    try:
        import math
        # 1. Prepare request payload
        r_deg = math.sqrt(max(100.0, area_m2)) / 111320.0
        footprint = {
            "type": "Polygon",
            "coordinates": [[
                [lon - r_deg, lat - r_deg],
                [lon + r_deg, lat - r_deg],
                [lon + r_deg, lat + r_deg],
                [lon - r_deg, lat + r_deg],
                [lon - r_deg, lat - r_deg],
            ]]
        }
        
        payload = {
            "detection_time": detection_time.isoformat(),
            "observed_footprint": footprint
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 2. Submit job
            resp = await client.post("http://127.0.0.1:8012/api/v1/origin/infer", json=payload)
            if resp.status_code != 202:
                log.error(f"Failed to submit origin inference job: {resp.text}")
                return None
                
            job_id = resp.json()["job_id"]
            log.info(f"Origin inference job {job_id} submitted")
            
            # 3. Poll for completion
            for _ in range(60): # 60 seconds timeout
                await asyncio.sleep(1.0)
                status_resp = await client.get(f"http://127.0.0.1:8012/api/v1/origin/jobs/{job_id}")
                if status_resp.status_code == 200:
                    status_data = status_resp.json()
                    status = status_data.get("status")
                    if status == "completed":
                        result_dict = status_data.get("result")
                        
                        # Wrap dictionary into object-like for compatibility with worker.py
                        class DummyResult:
                            pass
                        
                        origin_prob = DummyResult()
                        origin_prob.spatial_posterior = DummyResult()
                        origin_prob.temporal_posterior = DummyResult()
                        
                        sp = result_dict["spatial_posterior"]
                        tp = result_dict["temporal_posterior"]
                        
                        origin_prob.spatial_posterior.map_lat = sp["map_lat"]
                        origin_prob.spatial_posterior.map_lon = sp["map_lon"]
                        origin_prob.temporal_posterior.map_release_time = datetime.fromisoformat(tp["map_release_time"].replace("Z", "+00:00"))
                        
                        return origin_prob
                        
                    elif status == "failed":
                        log.error(f"Origin inference job failed: {status_data}")
                        return None
                        
        log.error("Origin inference job timed out")
        return None

    except Exception as e:
        log.error("Exception computing origin probability via API: %s", e)
        return None
