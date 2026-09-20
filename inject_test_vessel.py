#!/usr/bin/env python3
"""
inject_test_vessel.py

Injects a randomised synthetic HIGH-risk AIS vessel into the pipeline to test
the SAR + EO tasking chain end-to-end. Each run produces a fresh MMSI,
vessel name, type, position and anomaly profile so nothing repeats.

Run inside Docker (recommended — has DB + Redis access):
  docker cp inject_test_vessel.py aqua-sentinal-backend-1:/tmp/inject_test_vessel.py
  docker exec aqua-sentinal-backend-1 python3 /tmp/inject_test_vessel.py
"""

import asyncio
import json
import os
import random
import string
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as aioredis

# ── Connection config ─────────────────────────────────────────────────────────
PG_HOST = os.environ.get("POSTGRES_HOST", "postgres")
PG_PORT = int(os.environ.get("POSTGRES_PORT", 5432))
PG_USER = os.environ.get("POSTGRES_USER", "postgres")
PG_PASS = os.environ.get("POSTGRES_PASSWORD", "postgres")
PG_DB   = os.environ.get("POSTGRES_DB", "maritime_oilspill")

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))

# ── Randomised vessel parameters ──────────────────────────────────────────────
# MMSI: 999 prefix = clearly synthetic, last 6 digits random
MMSI = "999" + "".join(random.choices(string.digits, k=6))

VESSEL_TYPES   = ["tanker", "cargo", "fishing", "other"]
VESSEL_TYPE    = random.choice(VESSEL_TYPES)

NAME_PREFIXES  = ["SYNTHETIC", "TEST", "DEMO", "MOCK"]
NAME_SUFFIXES  = ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO",
                  "FOXTROT", "GOLF", "HOTEL", "INDIA", "JULIET"]
VESSEL_NAME    = f"{random.choice(NAME_PREFIXES)}-{VESSEL_TYPE.upper()}-{random.choice(NAME_SUFFIXES)}"

FLAGS          = ["XX", "ZZ", "YY", "WW"]
FLAG           = random.choice(FLAGS)

# Position: random scatter across Mumbai outer harbour / anchorage zone
# Bounding box: lat 18.85–19.20, lon 72.70–72.95
LAT = round(random.uniform(18.85, 19.20), 5)
LON = round(random.uniform(72.70, 72.95), 5)

# Anomaly severity — always HIGH so SAR tasking fires
ANOMALY_POOL = [
    {
        "anomaly_type": "sudden_stop",
        "severity":     "HIGH",
        "evidence":     {
            "speed_drop_kn":  round(random.uniform(8.0, 15.0), 1),
            "duration_min":   random.randint(30, 120),
            "loitering_score": round(random.uniform(0.75, 0.99), 2),
        },
    },
    {
        "anomaly_type": "ais_gap",
        "severity":     "HIGH",
        "evidence":     {
            "gap_minutes":           random.randint(60, 180),
            "last_known_speed_kn":   round(random.uniform(5.0, 14.0), 1),
        },
    },
    {
        "anomaly_type": "erratic_course",
        "severity":     "HIGH",
        "evidence":     {
            "course_variance_deg2":  round(random.uniform(3000, 8000), 1),
            "turn_reversals":        random.randint(4, 12),
        },
    },
    {
        "anomaly_type": "dark_rendezvous",
        "severity":     "HIGH",
        "evidence":     {
            "proximity_m":           random.randint(100, 500),
            "ais_gap_both_vessels":  True,
            "duration_min":          random.randint(20, 90),
        },
    },
]
# Pick 2 distinct anomaly types at random
ANOMALIES = random.sample(ANOMALY_POOL, 2)

NOW = datetime.now(timezone.utc)


async def main():
    print(f"\n{'='*55}")
    print(f"  Synthetic vessel injection")
    print(f"{'='*55}")
    print(f"  MMSI        : {MMSI}")
    print(f"  Name        : {VESSEL_NAME}")
    print(f"  Type        : {VESSEL_TYPE}")
    print(f"  Position    : lat={LAT}, lon={LON}")
    print(f"  Anomalies   : {', '.join(a['anomaly_type'] for a in ANOMALIES)}")
    print(f"{'='*55}\n")

    pool = await asyncpg.create_pool(
        host=PG_HOST, port=PG_PORT,
        user=PG_USER, password=PG_PASS,
        database=PG_DB, min_size=1, max_size=2,
    )
    redis = await aioredis.from_url(
        f"redis://{REDIS_HOST}:{REDIS_PORT}", decode_responses=True
    )

    # ── 1. Insert vessel ──────────────────────────────────────────────────────
    print("[+] Inserting vessel ...")
    vessel_id = await pool.fetchval("""
        INSERT INTO vessels (mmsi, name, vessel_type, flag, last_lat, last_lon, last_seen)
        VALUES ($1, $2, $3::vessel_type_enum, $4, $5, $6, $7)
        ON CONFLICT (mmsi) DO UPDATE SET
            name        = EXCLUDED.name,
            vessel_type = EXCLUDED.vessel_type,
            last_lat    = EXCLUDED.last_lat,
            last_lon    = EXCLUDED.last_lon,
            last_seen   = EXCLUDED.last_seen
        RETURNING id
    """, MMSI, VESSEL_NAME, VESSEL_TYPE, FLAG, LAT, LON, NOW)
    print(f"    vessel_id = {vessel_id}")

    # ── 2. Insert position ────────────────────────────────────────────────────
    print("[+] Inserting position ...")
    speed = round(random.uniform(0.0, 1.5), 1)   # near-stopped vessel
    course = round(random.uniform(0, 359), 1)
    await pool.execute("""
        INSERT INTO vessel_positions
            (vessel_id, timestamp, latitude, longitude,
             speed_knots, course_deg, nav_status, source)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'synthetic')
    """, vessel_id, NOW, LAT, LON, speed, course, "moored")

    # ── 3. Insert anomaly events ──────────────────────────────────────────────
    print("[+] Inserting anomaly events ...")
    for a in ANOMALIES:
        await pool.execute("""
            INSERT INTO anomaly_events
                (vessel_id, mmsi, window_start, anomaly_type, severity,
                 evidence, source, latitude, longitude, confidence_score)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)
            ON CONFLICT DO NOTHING
        """,
            vessel_id, MMSI, NOW,
            a["anomaly_type"], a["severity"],
            json.dumps(a["evidence"]),
            "synthetic_inject",
            LAT, LON, round(random.uniform(0.82, 0.98), 2),
        )
        print(f"    {a['anomaly_type']} / {a['severity']}")

    # ── 4. Publish to anomaly.events → risk-engine → SAR tasking ─────────────
    print("[+] Publishing to anomaly.events stream ...")
    for a in ANOMALIES:
        msg_id = await redis.xadd("anomaly.events", {
            "mmsi":             MMSI,
            "anomaly_type":     a["anomaly_type"],
            "severity":         a["severity"],
            "window_start":     NOW.isoformat(),
            "source":           "synthetic_inject",
            "evidence":         json.dumps(a["evidence"]),
            "latitude":         str(LAT),
            "longitude":        str(LON),
            "confidence_score": str(round(random.uniform(0.82, 0.98), 2)),
        }, maxlen=50000, approximate=True)
        print(f"    {msg_id}  ({a['anomaly_type']})")

    print(f"""
✓ Done.

  Risk engine will score MMSI {MMSI} → HIGH/CRITICAL → satellite_tasking_request

  Check in ~3s:
    curl http://localhost:8015/vessels/{MMSI}/risk
    curl http://localhost:8015/vessels/{MMSI}
""")

    await pool.close()
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
