"""Append one clearly labelled synthetic incident for Authority Dashboard demos.

Unlike seed_demo_data.py, this script never truncates or modifies existing
operational records. It publishes a real ``spill.severity`` event after the
PostGIS inserts so the Response Decision Engine creates recommendations itself.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import redis.asyncio as redis

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.db.connection import close_pool, create_pool


DEMO_SOURCE_IMAGE = "AUTHORITY_DASHBOARD_SYNTHETIC_DEMO"


async def main() -> int:
    pool = await create_pool()
    client = redis.from_url(
        f"redis://{os.environ.get('REDIS_HOST', 'localhost')}:{os.environ.get('REDIS_PORT', '6380')}",
        decode_responses=True,
    )
    try:
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT id FROM spill_incidents WHERE source_image_id=$1", DEMO_SOURCE_IMAGE
            )
            if existing:
                # Only replace this script's explicitly labelled demo row. The
                # cascade removes its dependent forecast/severity/attribution
                # records and cannot affect any operational incident.
                await conn.execute("DELETE FROM spill_incidents WHERE id=$1", existing)
                await conn.execute("DELETE FROM protected_areas WHERE name='DEMO ONLY — Authority response zone'")

            vessel = await conn.fetchrow(
                """SELECT id, mmsi, last_lat, last_lon FROM vessels
                   WHERE last_lat IS NOT NULL AND last_lon IS NOT NULL
                   ORDER BY last_seen DESC NULLS LAST LIMIT 1"""
            )
            if not vessel:
                raise RuntimeError("Cannot seed an incident: no positioned vessel is available")
            lat, lon = float(vessel["last_lat"] + 0.035), float(vessel["last_lon"] + 0.035)
            polygon = f"POLYGON(({lon - .025} {lat - .018},{lon + .025} {lat - .018},{lon + .025} {lat + .018},{lon - .025} {lat + .018},{lon - .025} {lat - .018}))"
            spill_id = uuid.uuid4()
            async with conn.transaction():
                await conn.execute(
                """INSERT INTO spill_incidents (id, latitude, longitude, geom, centroid, area_km2, confidence, source, source_image_id, status)
                   VALUES ($1,$2,$3,ST_GeomFromText($4,4326),ST_SetSRID(ST_MakePoint($3,$2),4326),$5,$6,'sar_satellite',$7,'detected')""",
                    spill_id, lat, lon, polygon, 8.4, 0.91, DEMO_SOURCE_IMAGE,
                )
                await conn.execute(
                    """INSERT INTO severity (spill_id, severity_level, score, environmental_risk, population_risk, economic_risk, protected_area_risk)
                       VALUES ($1,'HIGH',0.89,0.84,0.58,0.51,0.76)""", spill_id,
                )
                await conn.execute(
                    """INSERT INTO attribution_results (spill_id, vessel_id, distance_score, trajectory_score, wind_score, time_score, behavior_score, final_score, model_version)
                       VALUES ($1,$2,0.86,0.78,0.73,0.81,0.65,0.82,'authority-demo-v1')""", spill_id, vessel["id"],
                )
                for horizon in (3, 6, 12, 24, 48, 72):
                    shifted = f"POLYGON(({lon - .025 + horizon*.001} {lat - .018 + horizon*.0007},{lon + .025 + horizon*.001} {lat - .018 + horizon*.0007},{lon + .025 + horizon*.001} {lat + .018 + horizon*.0007},{lon - .025 + horizon*.001} {lat + .018 + horizon*.0007},{lon - .025 + horizon*.001} {lat - .018 + horizon*.0007}))"
                    await conn.execute(
                        """INSERT INTO forecasts (spill_id, forecast_time, horizon_hours, geom, confidence, model_version)
                           VALUES ($1,NOW()+($2::text || ' hours')::interval,$2::numeric,ST_GeomFromText($3,4326),$4,'authority-demo-v1')""",
                        spill_id, str(horizon), shifted, max(.42, .90 - horizon * .005),
                    )
                await conn.execute(
                    """INSERT INTO protected_areas (name, area_type, geom, metadata)
                       VALUES ('DEMO ONLY — Authority response zone','marine_protected_area',ST_Multi(ST_GeomFromText($1,4326)),'{"synthetic_demo": true}'::jsonb)""", polygon,
                )

        await client.xadd("spill.severity", {
            "spill_id": str(spill_id), "spill_lat": str(lat), "spill_lon": str(lon),
            "severity_level": "HIGH", "severity_score": "0.89", "protected_area_risk": "0.76",
            "population_risk": "0.58", "protected_areas_nearby": "1", "coast_distance_m": "4200",
            "is_synthetic": "true",
        })
        print(f"Inserted append-only synthetic Authority Dashboard incident: {spill_id}")
        print("Published spill.severity; Response Decision Engine will generate the recommendations.")
        return 0
    finally:
        await client.aclose()
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
