import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.db.connection import close_pool, create_pool

NOW = datetime.now(timezone.utc)


def box(lon, lat, h=0.05):
    return (
        f"POLYGON(({lon - h:.4f} {lat - h:.4f}, {lon + h:.4f} {lat - h:.4f}, "
        f"{lon + h:.4f} {lat + h:.4f}, {lon - h:.4f} {lat + h:.4f}, "
        f"{lon - h:.4f} {lat - h:.4f}))"
    )


SPILL_LON = 73.9
SPILL_LAT = 15.2

VESSELS = [
    (419000001, "1234567", "Aqua Tanker", "tanker", "India", 250, 32, 45000, "Example Shipping Co"),
    (419000002, None, "Coastal Trader", "cargo", "India", 150, 24, 12000, "Example Logistics Ltd"),
    (419000003, None, "Ocean Harvester", "fishing", "India", 45, 10, 300, "Village Fishermen Co-op"),
]


async def main() -> int:
    pool = await create_pool()
    try:
        conn = await pool.acquire()
        try:
            async with conn.transaction():
                await conn.execute(
                    "TRUNCATE vessels, vessel_positions, spill_incidents, "
                    "attribution_results, forecasts, severity, "
                    "response_recommendations, protected_areas, "
                    "environmental_conditions RESTART IDENTITY CASCADE"
                )

                vessel_ids = []
                for mmsi, imo, name, vtype, flag, length, width, gt, operator in VESSELS:
                    vid = await conn.fetchval(
                        """
                        INSERT INTO vessels (imo_number, mmsi, name, vessel_type, flag,
                                             length_m, width_m, gross_tonnage, operator,
                                             created_at, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                        RETURNING id
                        """,
                        imo, str(mmsi), name, vtype, flag, length, width, gt, operator, NOW, NOW,
                    )
                    vessel_ids.append(vid)

                speeds = {str(v[0]): 10 for v in VESSELS}
                speeds[str(VESSELS[2][0])] = 5
                positions = 0
                for idx, (mmsi, *_) in enumerate(VESSELS):
                    for i in range(5):
                        t = NOW - timedelta(hours=6 - i)
                        lat = SPILL_LAT - 0.05 + i * 0.02
                        lon = SPILL_LON - 0.10 + i * 0.02
                        await conn.execute(
                            """
                            INSERT INTO vessel_positions (vessel_id, timestamp, latitude,
                                                          longitude, geom, speed_knots,
                                                          course_deg, heading_deg,
                                                          nav_status, source)
                            VALUES ($1, $2, $3, $4, ST_SetSRID(ST_MakePoint($5, $6), 4326),
                                    $7, $8, $9, $10, $11)
                            """,
                            vessel_ids[idx], t, lat, lon, lon, lat, speeds[str(mmsi)], 120, 125, "under way using engine", "simulator",
                        )
                        positions += 1

                spill_id = uuid.uuid4()
                await conn.execute(
                    """
                    INSERT INTO spill_incidents (id, detected_at, latitude, longitude,
                                                 geom, centroid, area_km2, confidence,
                                                 source, source_image_id, status)
                    VALUES ($1, $2, $3, $4, ST_GeomFromText($5, 4326),
                            ST_SetSRID(ST_MakePoint($6, $7), 4326), $8, $9, $10, $11, $12)
                    """,
                    spill_id, NOW, SPILL_LAT, SPILL_LON, box(SPILL_LON, SPILL_LAT),
                    SPILL_LON, SPILL_LAT, 12.5, 0.92, "SAR", "S1A_20260819T0100", "detected",
                )

                candidates = [
                    (vessel_ids[0], 0.90, 0.85, 0.70, 0.80, 0.60, 0.82),
                    (vessel_ids[2], 0.50, 0.40, 0.55, 0.30, 0.60, 0.44),
                ]
                for vid, dist, traj, wind, tscore, behav, final in candidates:
                    await conn.execute(
                        """
                        INSERT INTO attribution_results (spill_id, vessel_id, distance_score,
                                                          trajectory_score, wind_score, time_score,
                                                          behavior_score, final_score, model_version,
                                                          computed_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        """,
                        spill_id, vid, dist, traj, wind, tscore, behav, final, "v1", NOW,
                    )

                for horizon in (1, 3, 6, 12, 24):
                    await conn.execute(
                        """
                        INSERT INTO forecasts (spill_id, forecast_time, generated_at,
                                               horizon_hours, geom, model_version, confidence)
                        VALUES ($1, $2, $3, $4, ST_GeomFromText($5, 4326), $6, $7)
                        """,
                        spill_id, NOW + timedelta(hours=horizon), NOW, horizon,
                        box(SPILL_LON + horizon * 0.005, SPILL_LAT + horizon * 0.003),
                        "drift-v1", 0.80,
                    )

                await conn.execute(
                    """
                    INSERT INTO severity (spill_id, severity_level, score, environmental_risk,
                                          population_risk, economic_risk, protected_area_risk,
                                          computed_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    spill_id, "HIGH", 8.5, 0.90, 0.70, 0.60, 0.85, NOW,
                )

                for rec, priority in (
                    ("Deploy containment booms around spill perimeter", "URGENT"),
                    ("Dispatch surveillance drone for tracking", "HIGH"),
                ):
                    await conn.execute(
                        """
                        INSERT INTO response_recommendations (spill_id, recommendation, priority,
                                                              status, generated_at)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        spill_id, rec, priority, "pending", NOW,
                    )

                await conn.execute(
                    """
                    INSERT INTO protected_areas (name, area_type, geom, metadata)
                    VALUES ($1, $2, ST_GeomFromText($3, 4326), $4)
                    """,
                    "Kundapura Marine Protected Area", "marine_protected_area",
                    "MULTIPOLYGON(((73.70 15.05, 74.10 15.05, 74.10 15.40, 73.70 15.40, 73.70 15.05)))",
                    json.dumps({"country": "India", "habitat": "mangrove", "protection_level": "strict"}),
                )

                for i in range(4):
                    t = NOW + timedelta(hours=i * 6)
                    await conn.execute(
                        """
                        INSERT INTO environmental_conditions (timestamp, latitude, longitude,
                                                              geom, wind_speed_kmh, wind_direction_deg,
                                                              current_speed_ms, current_direction_deg,
                                                              source)
                        VALUES ($1, $2, $3, ST_SetSRID(ST_MakePoint($4, $5), 4326),
                                $6, $7, $8, $9, $10)
                        """,
                        t, SPILL_LAT + 0.05, SPILL_LON + 0.05, SPILL_LON + 0.05, SPILL_LAT + 0.05,
                        12, 240, 0.8, 210, "era5",
                    )

                print("Demo data inserted:")
                print(f"  vessels                  : {len(vessel_ids)}")
                print(f"  vessel_positions         : {positions}")
                print(f"  spill_incidents          : 1 ({spill_id})")
                print(f"  attribution_results      : {len(candidates)}")
                print(f"  forecasts                : 5")
                print(f"  severity                 : 1")
                print(f"  response_recommendations : 2")
                print(f"  protected_areas          : 1")
                print(f"  environmental_conditions : 4")
                return 0
        finally:
            await pool.release(conn)
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))