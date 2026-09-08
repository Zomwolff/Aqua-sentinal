import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
import pandas as pd

import sys
import os

services_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "services"))
sys.path.insert(0, services_dir)

from shared.db import get_pool, upsert_vessel, insert_position, get_or_create_vessel_id
from shared.redis_client import get_redis, publish_to_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("synthetic-ais")

async def inject_row(pool, redis, row):
    try:
        mmsi = str(int(row["MMSI"]))
        lat = float(row["LAT"])
        lon = float(row["LON"])
        sog = float(row["SOG"]) if pd.notnull(row["SOG"]) else None
        cog = float(row["COG"]) if pd.notnull(row["COG"]) else None
        heading = float(row["Heading"]) if pd.notnull(row["Heading"]) else None
        
        imo_val = row.get("IMO")
        imo_number = None
        if pd.notnull(imo_val):
            try:
                imo_number = int(float(imo_val))
            except ValueError:
                pass
                
        record = {
            "mmsi": mmsi,
            "lat": lat,
            "lon": lon,
            "speed_knots": sog,
            "course": cog,
            "heading": heading,
            "nav_status": None,
            "timestamp": datetime.now(timezone.utc),
            "vessel_name": str(row["VesselName"]) if pd.notnull(row.get("VesselName")) else None,
            "imo_number": imo_number,
            "vessel_type": None,
            "vessel_type_str": str(row["VesselType"]) if pd.notnull(row.get("VesselType")) else "unknown",
            "raw_source": "synthetic",
            "quality_flag": "raw",
            "call_sign": str(row["CallSign"]) if pd.notnull(row.get("CallSign")) else None,
            "draught": None
        }

        vessel_id = await upsert_vessel(pool, record)
        if vessel_id is None:
            vessel_id = await get_or_create_vessel_id(pool, mmsi)
        if vessel_id is None:
            return

        await insert_position(pool, vessel_id, record)

        await publish_to_stream(redis, "ais.clean", {
            "mmsi": mmsi,
            "vessel_id": str(vessel_id),
            "lat": record["lat"],
            "lon": record["lon"],
            "speed_knots": record["speed_knots"] or "",
            "course": record["course"] or "",
            "heading": record["heading"] or "",
            "nav_status": "",
            "timestamp": record["timestamp"].isoformat(),
            "vessel_name": record["vessel_name"] or "",
            "vessel_type_str": record["vessel_type_str"] or "unknown",
            "quality_flag": record["quality_flag"],
            "raw_source": record["raw_source"],
            "draught": record["draught"] or "",
        })
    except Exception as e:
        log.error(f"Error injecting MMSI {row.get('MMSI')}: {e}")

async def main():
    log.info("Loading synthetic CSV...")
    csv_path = os.path.join(os.path.dirname(__file__), "synthetic-mumbai-ais.csv")
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        log.error(f"Failed to load synthetic CSV: {e}")
        return
        
    # Sort chronologically by the historical timestamp to perfectly maintain real trajectory order
    df['BaseDateTime'] = pd.to_datetime(df['BaseDateTime'])
    df = df.sort_values(by='BaseDateTime')
    
    log.info(f"Loaded {len(df)} rows. Connecting to DB & Redis...")
    pool = await get_pool()
    redis = await get_redis()
    
    log.info("Starting injection loop (30 vessels per 60s)...")
    
    # Group rows into chunks of 30
    chunk_size = 30
    for i in range(0, len(df), chunk_size):
        chunk = df.iloc[i:i+chunk_size]
        
        log.info(f"Injecting chunk {i//chunk_size + 1} / {(len(df)//chunk_size)+1}: {len(chunk)} vessels.")
        tasks = []
        for _, row in chunk.iterrows():
            tasks.append(inject_row(pool, redis, row))
        
        if tasks:
            await asyncio.gather(*tasks)
            
        # Real-time pacing: Sleep exactly 60 seconds to match the required synthetic rate
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
