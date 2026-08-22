import asyncio
import json
from datetime import datetime, timezone
import asyncpg

async def main():
    conn = await asyncpg.connect(
        user="postgres",
        password="postgres",
        host="localhost",
        port=5433,
        database="maritime_oilspill"
    )
    with open('vessels.json', 'r') as f:
        data = json.load(f)
    v = data['vessels'][0]
    mmsi = str(v['mmsi'])
    print(f"Inserting {mmsi}")
    
    await conn.execute("""
        INSERT INTO vessels (mmsi, name, vessel_type, flag, imo_number, destination, last_lat, last_lon, last_seen)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (mmsi) DO NOTHING
    """, mmsi, v.get('vessel_name', 'Unknown'), 'unknown', v.get('flag', 'XX'),
    str(v.get('imo_number')) if v.get('imo_number') else None,
    v.get('destination', ''), float(v['last_lat']), float(v['last_lon']), datetime.now(timezone.utc))
    
    v_id = await conn.fetchval("SELECT id FROM vessels WHERE mmsi=$1", mmsi)
    
    await conn.execute("""
        INSERT INTO vessel_positions (vessel_id, timestamp, latitude, longitude, speed_knots, course_deg, heading_deg)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
    """, v_id, datetime.now(timezone.utc), float(v['last_lat']), float(v['last_lon']), 12.0, 90.0, 90.0)
    
    print("Inserted!")
    await conn.close()

asyncio.run(main())
