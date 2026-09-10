"""Backfill only missing cost projections after applying the response migration.
Run inside the backend container, which supplies the existing cost/dispatch model.
"""
import asyncio
import json
import sys
sys.path.insert(0, '/svc/response-decision')
from shared.db.connection import create_pool, close_pool
from app.cost_model import project_cost
from app.worker import find_nearest_certified_vessels

async def main():
    pool=await create_pool()
    rows=await pool.fetch('''SELECT i.id,i.area_km2,i.latitude,i.longitude,s.severity_level,s.population_risk
        FROM spill_incidents i JOIN severity s ON s.spill_id=i.id
        WHERE NOT EXISTS (SELECT 1 FROM cost_projections c WHERE c.spill_id=i.id)''')
    for row in rows:
        cost=project_cost(float(row['area_km2']),str(row['severity_level']),float(row['population_risk'] or 0))
        vessels=await find_nearest_certified_vessels(pool,float(row['latitude']),float(row['longitude']))
        await pool.execute('''INSERT INTO cost_projections
            (spill_id,nosdcp_tier,estimated_volume_tonnes,point_usd,low_usd,high_usd,point_inr,low_inr,high_inr,cost_curve,matched_vessels)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb)''',row['id'],
            *[cost[k] for k in ('nosdcp_tier','estimated_volume_tonnes','point_usd','low_usd','high_usd','point_inr','low_inr','high_inr')],
            json.dumps(cost['cost_curve']),json.dumps(vessels))
    print(f'Backfilled {len(rows)} missing cost projections; existing recommendations preserved.')
    await close_pool()

if __name__=='__main__': asyncio.run(main())
