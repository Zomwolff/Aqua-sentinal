import asyncio
import json
import asyncpg

async def seed():
    conn = await asyncpg.connect(
        user="aqua_sentinel",
        password="change_me",
        host="localhost",
        port=5433,
        database="maritime_oilspill"
    )
    
    mmsi = 999999999
    
    # 1. Ensure vessel exists
    vessel = await conn.fetchrow("SELECT id FROM vessels WHERE mmsi=$1", str(mmsi))
    if not vessel:
        print(f"Inserting vessel {mmsi}...")
        vessel_id = await conn.fetchval(
            """
            INSERT INTO vessels (mmsi, name, vessel_type, last_seen, last_lat, last_lon)
            VALUES ($1, $2, $3, NOW(), 18.93978, 72.86613)
            RETURNING id
            """,
            str(mmsi), "FLAGGED-TANKER-Y", "tanker"
        )
    else:
        vessel_id = vessel["id"]
        
    # 2. Insert or update risk score
    print("Setting risk score...")
    factors = json.dumps([
        {"factor": "AIS Gap", "weight": 0.8, "value": 1.0, "contribution": 40.0},
        {"factor": "Loitering", "weight": 0.6, "value": 0.9, "contribution": 30.0},
        {"factor": "Spoofing", "weight": 0.9, "value": 1.0, "contribution": 25.0}
    ])
    await conn.execute(
        """
        INSERT INTO vessel_risk_scores (vessel_id, mmsi, risk_score, tier, contributing_factors, recommended_action)
        VALUES ($1, $2, 98.0, 'CRITICAL', $3, 'Immediate SAR Tasking')
        ON CONFLICT (mmsi) DO UPDATE SET
            risk_score = EXCLUDED.risk_score,
            tier = EXCLUDED.tier,
            contributing_factors = EXCLUDED.contributing_factors,
            recommended_action = EXCLUDED.recommended_action
        """,
        vessel_id, str(mmsi), factors
    )
    
    # 3. Insert satellite tasking request
    print("Creating satellite tasking request...")
    await conn.execute(
        """
        INSERT INTO satellite_tasking_requests (vessel_id, mmsi, risk_score, risk_tier, status, scene_id)
        VALUES ($1, $2, 98.0, 'CRITICAL', 'pending', NULL)
        """,
        vessel_id, str(mmsi)
    )
    
    print("Done seeding flagged vessel.")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(seed())
