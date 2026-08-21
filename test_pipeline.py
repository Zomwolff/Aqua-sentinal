import asyncio
import uuid
import time
from datetime import datetime
import json
import sys

sys.path.insert(0, "./services")
from shared.redis_client import get_redis, publish_to_stream, close_redis
from shared.db.connection import get_pool, create_pool, close_pool

async def run_test():
    pool = await create_pool()
    redis = await get_redis()
    
    # 1. Inject a fake spill into incident.fused
    spill_id = f"test-spill-{uuid.uuid4().hex[:8]}"
    print(f"Injecting spill: {spill_id}")
    
    await publish_to_stream(redis, "incident.fused", {
        "candidate_id": spill_id,
        "lat": 18.95,
        "lon": 72.88,
        "area_km2": 5.0,
        "confidence": 0.85,
        "is_synthetic": True
    })
    
    # 2. Wait for workers to process
    print("Waiting 5 seconds for workers to process...")
    await asyncio.sleep(5)
    
    # 3. Query DB to check results
    print("\n--- DB RESULTS ---")
    
    # Check spill_incidents
    spill = await pool.fetchrow("SELECT id, area_km2 FROM spill_incidents WHERE id = $1", spill_id)
    if spill:
        print(f"✅ spill_incidents: FOUND (area_km2: {spill['area_km2']})")
    else:
        print("❌ spill_incidents: NOT FOUND")
        
    # Check attribution_results
    attrs = await pool.fetch("SELECT vessel_id, final_score FROM attribution_results WHERE spill_id = $1", spill_id)
    print(f"✅ attribution_results: {len(attrs)} records found")
    
    # Check forecasts
    forecasts = await pool.fetch("SELECT horizon_hours FROM forecasts WHERE spill_id = $1", spill_id)
    print(f"✅ forecasts: {len(forecasts)} records found")
    
    # Check severity
    sev = await pool.fetchrow("SELECT severity_level, score FROM severity WHERE spill_id = $1", spill_id)
    if sev:
        print(f"✅ severity: FOUND (level: {sev['severity_level']}, score: {sev['score']})")
    else:
        print("❌ severity: NOT FOUND")
        
    # Check response_recommendations
    recs = await pool.fetch("SELECT priority, recommendation FROM response_recommendations WHERE spill_id = $1", spill_id)
    print(f"✅ response_recommendations: {len(recs)} records found")
    if recs:
        print(f"   Top recommendation: [{recs[0]['priority']}] {recs[0]['recommendation'][:80]}...")
        
    await close_redis()
    await close_pool()

if __name__ == "__main__":
    asyncio.run(run_test())
