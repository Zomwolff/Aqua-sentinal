import asyncio
import json
import asyncpg

import random

async def seed():
    conn = await asyncpg.connect(
        user="postgres",
        password="postgres",
        host="postgres",
        port=5432,
        database="maritime_oilspill"
    )
    
    print("Fetching a real vessel from the database...")
    rows = await conn.fetch("""
        SELECT v.id as vessel_id, v.mmsi, p.latitude, p.longitude 
        FROM vessel_positions p 
        JOIN vessels v ON p.vessel_id = v.id 
        WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL
        ORDER BY p.timestamp DESC 
        LIMIT 50
    """)
    
    if not rows:
        print("No vessels found with positions!")
        return
        
    row = random.choice(rows)
    
    vessel_id = row['vessel_id']
    mmsi = row['mmsi']
    lat = row['latitude']
    lon = row['longitude']
    
    print(f"Selected live vessel {mmsi} at coordinates ({lat}, {lon})")
    
    print("Setting risk score to CRITICAL...")
    factors = json.dumps([
        {"factor": "AIS Gap", "weight": 0.8, "value": 1.0, "contribution": 40.0},
        {"factor": "Loitering", "weight": 0.6, "value": 0.9, "contribution": 30.0},
        {"factor": "Spoofing", "weight": 0.9, "value": 1.0, "contribution": 25.0}
    ])
    await conn.execute("""
        INSERT INTO vessel_risk_scores (vessel_id, mmsi, risk_score, tier, contributing_factors, recommended_action, updated_at)
        VALUES ($1, $2, 98, 'CRITICAL', $3, 'SAR Tasking: Requested', CURRENT_TIMESTAMP)
        ON CONFLICT (vessel_id) DO UPDATE 
        SET risk_score = 98, tier = 'CRITICAL', contributing_factors=$3, updated_at = CURRENT_TIMESTAMP
    """, vessel_id, str(mmsi), factors)
    
    print("Creating satellite tasking request for the real coordinates...")
    await conn.execute("""
        INSERT INTO satellite_tasking_requests (vessel_id, mmsi, risk_score, risk_tier, status, scene_id)
        VALUES ($1, $2, 98.0, 'CRITICAL', 'pending', NULL)
    """, vessel_id, str(mmsi))
    
    print(f"Successfully flagged real vessel {mmsi} and triggered SAR pipeline at ({lat}, {lon}).")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(seed())
