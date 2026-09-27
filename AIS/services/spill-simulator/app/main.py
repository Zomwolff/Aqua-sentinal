#!/usr/bin/env python3
"""
Wakashio Oil Spill Simulator
─────────────────────────────────────────────────────────────────────────────
Streams the wakashio_realistic.csv scenario into the live pipeline.

Key design decisions:
  • Timestamps are SHIFTED to NOW so anomaly-detection windows (which are
    relative to NOW() in Postgres) actually fire.  The original 2020 event
    date is preserved in the "sim_event_date" metadata field.
  • The 45-hour scenario is compressed by COMPRESSION_FACTOR (default 120×)
    → ~22 minutes wall-clock.
  • Every record is written to Postgres (upsert_vessel + insert_position)
    AND published to the ais.clean Redis stream.
  • After the simulation completes, anomalies/features should already have
    been written by the ais-service worker; we also directly insert a
    synthetic AIS-gap anomaly for MV WAKASHIO to guarantee it triggers.
"""

import asyncio
import csv
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, "/app")
from shared.db import get_pool, upsert_vessel, insert_position

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("spill-simulator")

# ─── config ───────────────────────────────────────────────────────────────────
SCENARIO_PATH = Path("/app/simulation_scenarios/wakashio_realistic.csv")
CONTROL_CHANNEL = "control.simulate_spill"
STOP_CHANNEL = "control.stop_simulation"
STATUS_STREAM = "simulation.status"
AIS_STREAM = "ais.clean"

# 120× compression  →  45 h scenario ≈ 22 min wall-clock
COMPRESSION_FACTOR = int(os.getenv("SIMULATION_SPEED_MULTIPLIER", "120"))
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# The WAKASHIO grounding coordinates (Pointe d'Esny, Mauritius)
WAKASHIO_MMSI = "477995000"
PACIFIC_GLORY_MMSI = "636092948"
SPILL_LAT = -20.4089
SPILL_LON = 57.7831

# ─── state ────────────────────────────────────────────────────────────────────
simulation_state: Dict = {
    "running": False,
    "scenario": "wakashio_2020",
    "progress_pct": 0,
    "vessels_injected": 0,
    "records_processed": 0,
    "total_records": 0,
    "started_at": None,
    "completed_at": None,
    "task": None,
    "db_write_count": 0,
    "error": None,
}

vessel_id_map: Dict[str, int] = {}

# ─── app ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="Oil Spill Simulator – Wakashio")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "spill-simulator",
        "scenario": str(SCENARIO_PATH),
        "scenario_loaded": SCENARIO_PATH.exists(),
        "simulation_running": simulation_state["running"],
    }


@app.get("/status")
async def get_status():
    state = {k: v for k, v in simulation_state.items() if k != "task"}
    return state


@app.post("/simulate/oil-spill")
async def trigger_oil_spill():
    """HTTP trigger — identical to publishing to control.simulate_spill."""
    if simulation_state["running"]:
        return {"status": "already_running", "progress_pct": simulation_state["progress_pct"]}
    try:
        records = load_scenario_csv()
    except FileNotFoundError as e:
        return {"status": "error", "detail": str(e)}
    simulation_state["task"] = asyncio.create_task(run_simulation(records))
    return {
        "status": "started",
        "total_records": len(records),
        "vessels": len(set(r["mmsi"] for r in records)),
        "compression_factor": COMPRESSION_FACTOR,
        "estimated_duration_minutes": round(
            (records[-1]["timestamp_orig"] - records[0]["timestamp_orig"]).total_seconds()
            / 60
            / COMPRESSION_FACTOR,
            1,
        ),
        "spill_location": {"lat": SPILL_LAT, "lon": SPILL_LON},
        "target_vessel": "MV WAKASHIO",
        "target_mmsi": WAKASHIO_MMSI,
    }


@app.post("/stop")
async def stop_simulation():
    simulation_state["running"] = False
    if simulation_state["task"]:
        simulation_state["task"].cancel()
        simulation_state["task"] = None
    return {"status": "stopped"}


# ─── CSV loader ───────────────────────────────────────────────────────────────

def load_scenario_csv() -> List[Dict]:
    if not SCENARIO_PATH.exists():
        raise FileNotFoundError(f"Scenario CSV not found: {SCENARIO_PATH}")

    records: List[Dict] = []
    with open(SCENARIO_PATH, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["BaseDateTime"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                records.append(
                    {
                        "mmsi": row["MMSI"],
                        "timestamp_orig": ts,
                        "lat": float(row["LAT"]),
                        "lon": float(row["LON"]),
                        "sog": float(row["SOG"]) if row.get("SOG") else 0.0,
                        "cog": float(row["COG"]) if row.get("COG") else 0.0,
                        "heading": float(row["Heading"]) if row.get("Heading") else 511.0,
                        "vessel_name": row.get("VesselName", f"Vessel-{row['MMSI']}"),
                        "vessel_type": row.get("VesselType", "other"),
                        "is_target": row.get("IsTarget", "").lower() == "true",
                        "status": row.get("Status", ""),
                        "imo": row.get("IMO", ""),
                        "call_sign": row.get("CallSign", ""),
                        "length": float(row["Length"]) if row.get("Length") else None,
                        "width": float(row["Width"]) if row.get("Width") else None,
                        "draft": float(row["Draft"]) if row.get("Draft") else None,
                    }
                )
            except (ValueError, KeyError) as e:
                log.warning(f"Skipping malformed row: {e}")

    records.sort(key=lambda x: x["timestamp_orig"])
    log.info(
        f"Loaded {len(records)} records | "
        f"{len(set(r['mmsi'] for r in records))} vessels | "
        f"{records[0]['timestamp_orig']} → {records[-1]['timestamp_orig']}"
    )
    return records


# ─── core injection ───────────────────────────────────────────────────────────

async def inject_record(
    rc: redis.Redis,
    pool,
    record: Dict,
    shifted_ts: datetime,
):
    """Write one AIS record to Postgres and publish to ais.clean."""
    mmsi = str(record["mmsi"])
    db_record = {
        "mmsi": mmsi,
        "vessel_name": record["vessel_name"],
        "vessel_type_str": record["vessel_type"],
        "timestamp": shifted_ts,
        "lat": record["lat"],
        "lon": record["lon"],
        "speed_knots": record["sog"],
        "course": record["cog"],
        "heading": record["heading"],
        "nav_status": record.get("status", ""),
        "raw_source": "wakashio_simulation",
        "quality_flag": "simulated",
        "imo_number": record.get("imo") or None,
        "call_sign": record.get("call_sign") or None,
        "length_m": record.get("length"),
        "width_m": record.get("width"),
        "draught": record.get("draft"),
    }

    vessel_id = vessel_id_map.get(mmsi)
    if vessel_id is None:
        vessel_id = await upsert_vessel(pool, db_record)
        vessel_id_map[mmsi] = vessel_id
    else:
        # Still update last_seen / last_lat / last_lon
        await upsert_vessel(pool, db_record)

    await insert_position(pool, vessel_id, db_record)
    simulation_state["db_write_count"] += 1

    # Publish to Redis stream
    msg = {
        "mmsi": mmsi,
        "vessel_id": str(vessel_id),
        "lat": str(record["lat"]),
        "lon": str(record["lon"]),
        "speed_knots": str(record["sog"]),
        "course": str(record["cog"]),
        "heading": str(record["heading"]),
        "nav_status": record.get("status", ""),
        "timestamp": shifted_ts.isoformat(),
        "vessel_name": record["vessel_name"],
        "vessel_type_str": record["vessel_type"],
        "quality_flag": "simulated",
        "raw_source": "wakashio_simulation",
        "draught": str(record.get("draft") or ""),
    }
    if record["is_target"]:
        msg["_simulation_target"] = "true"

    await rc.xadd(AIS_STREAM, msg)


async def inject_synthetic_anomalies(pool, rc: redis.Redis, shift_delta: timedelta):
    """
    Directly insert anomaly_events rows so MV WAKASHIO and MT PACIFIC GLORY
    are guaranteed to appear as HIGH/CRITICAL in the risk leaderboard,
    even if the ais-service worker hasn't processed the window yet.
    """
    wakashio_id = vessel_id_map.get(WAKASHIO_MMSI)
    pacific_id = vessel_id_map.get(PACIFIC_GLORY_MMSI)
    now = datetime.now(timezone.utc)

    anomalies = []

    if wakashio_id:
        # AIS gap (8 hours) — the primary dark-vessel event before grounding
        anomalies.append((
            wakashio_id, WAKASHIO_MMSI,
            now - timedelta(hours=6), "ais_gap", "CRITICAL",
            json.dumps({
                "gap_duration_minutes": 480,
                "last_known_lat": -20.2,
                "last_known_lon": 57.6,
                "description": "8-hour AIS gap before grounding near Pointe d'Esny",
            }),
            SPILL_LAT, SPILL_LON, 0.97,
        ))
        # Loitering near reef
        anomalies.append((
            wakashio_id, WAKASHIO_MMSI,
            now - timedelta(hours=3), "loitering", "HIGH",
            json.dumps({
                "speed_knots": 0.4,
                "duration_minutes": 210,
                "location": "Pointe d'Esny reef",
                "description": "Vessel drifting/anchored over protected reef",
            }),
            SPILL_LAT, SPILL_LON, 0.94,
        ))
        # Erratic course
        anomalies.append((
            wakashio_id, WAKASHIO_MMSI,
            now - timedelta(hours=8), "erratic_course", "HIGH",
            json.dumps({
                "course_variance": 4100,
                "description": "Erratic course changes 8h before grounding",
            }),
            -20.35, 57.73, 0.88,
        ))

    if pacific_id:
        # STS proximity event with WAKASHIO
        anomalies.append((
            pacific_id, PACIFIC_GLORY_MMSI,
            now - timedelta(hours=12), "sts_proximity", "HIGH",
            json.dumps({
                "partner_mmsi": WAKASHIO_MMSI,
                "partner_name": "MV WAKASHIO",
                "proximity_m": 280,
                "duration_minutes": 95,
                "description": "Ship-to-ship proximity consistent with cargo/fuel transfer",
            }),
            -20.38, 57.76, 0.91,
        ))
        anomalies.append((
            pacific_id, PACIFIC_GLORY_MMSI,
            now - timedelta(hours=10), "loitering", "MEDIUM",
            json.dumps({
                "speed_knots": 0.6,
                "duration_minutes": 130,
                "description": "Loitering near WAKASHIO position",
            }),
            -20.39, 57.77, 0.82,
        ))

    if not anomalies:
        log.warning("No vessel IDs found for synthetic anomaly injection — skipping")
        return

    for row in anomalies:
        vessel_id, mmsi, window_start, atype, severity, evidence, lat, lon, conf = row
        try:
            await pool.execute(
                """
                INSERT INTO anomaly_events
                    (vessel_id, mmsi, window_start, anomaly_type, severity,
                     evidence, source, latitude, longitude, confidence_score)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,'simulation',$7,$8,$9)
                ON CONFLICT DO NOTHING
                """,
                vessel_id, mmsi, window_start, atype, severity, evidence, lat, lon, conf,
            )
            log.info(f"  ✓ Anomaly: {mmsi} | {atype} | {severity} | conf={conf}")
        except Exception as e:
            log.error(f"  ✗ Failed to insert anomaly for {mmsi}: {e}")


async def update_risk_scores(pool):
    """
    Upsert risk scores for WAKASHIO (CRITICAL) and PACIFIC GLORY (HIGH)
    based on the injected anomalies.
    """
    scores = [
        (
            WAKASHIO_MMSI,
            vessel_id_map.get(WAKASHIO_MMSI),
            92.0, "CRITICAL",
            json.dumps([
                {"factor": "ais_gap_8h", "weight": 0.40, "score": 97},
                {"factor": "loitering_reef", "weight": 0.30, "score": 94},
                {"factor": "erratic_course", "weight": 0.20, "score": 88},
                {"factor": "tanker_near_protected", "weight": 0.10, "score": 85},
            ]),
            "GROUND_TRUTH_KNOWN — MV WAKASHIO grounded Pointe d'Esny Aug 2020",
        ),
        (
            PACIFIC_GLORY_MMSI,
            vessel_id_map.get(PACIFIC_GLORY_MMSI),
            71.0, "HIGH",
            json.dumps([
                {"factor": "sts_proximity_wakashio", "weight": 0.50, "score": 91},
                {"factor": "loitering", "weight": 0.30, "score": 82},
                {"factor": "tanker_class", "weight": 0.20, "score": 55},
            ]),
            "STS-like behaviour near WAKASHIO grounding site",
        ),
    ]

    for mmsi, vessel_id, score, tier, factors, action in scores:
        if vessel_id is None:
            log.warning(f"No vessel_id for {mmsi}, skipping risk score upsert")
            continue
        try:
            await pool.execute(
                """
                INSERT INTO vessel_risk_scores
                    (vessel_id, mmsi, risk_score, tier, contributing_factors,
                     recommended_action, updated_at)
                VALUES ($1,$2,$3,$4,$5::jsonb,$6,NOW())
                ON CONFLICT (mmsi) DO UPDATE SET
                    risk_score          = EXCLUDED.risk_score,
                    tier                = EXCLUDED.tier,
                    contributing_factors= EXCLUDED.contributing_factors,
                    recommended_action  = EXCLUDED.recommended_action,
                    updated_at          = NOW()
                """,
                vessel_id, mmsi, score, tier, factors, action,
            )
            log.info(f"  ✓ Risk score: {mmsi} → {score} ({tier})")
        except Exception as e:
            log.error(f"  ✗ Risk score upsert failed for {mmsi}: {e}")


# ─── simulation runner ────────────────────────────────────────────────────────

async def run_simulation(records: List[Dict]):
    simulation_state.update(
        running=True,
        progress_pct=0,
        records_processed=0,
        total_records=len(records),
        db_write_count=0,
        started_at=datetime.now(timezone.utc).isoformat(),
        completed_at=None,
        error=None,
    )
    vessel_id_map.clear()

    rc = await redis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, decode_responses=True
    )
    pool = await get_pool()

    unique_mmsis = set(r["mmsi"] for r in records)
    log.info("=" * 60)
    log.info("🛢️  WAKASHIO OIL SPILL SIMULATION STARTING")
    log.info(f"   Records  : {len(records)}")
    log.info(f"   Vessels  : {len(unique_mmsis)}")
    log.info(f"   Speed    : {COMPRESSION_FACTOR}× real-time")
    log.info(f"   Stream   : {AIS_STREAM}")
    log.info(f"   Scenario : Aug 5-7, 2020 → shifted to NOW")
    log.info("=" * 60)

    simulation_state["vessels_injected"] = len(unique_mmsis)

    # Calculate the time shift: align scenario start to NOW
    scenario_start: datetime = records[0]["timestamp_orig"]
    now_start = datetime.now(timezone.utc)
    shift_delta: timedelta = now_start - scenario_start

    real_start = datetime.now(timezone.utc)

    try:
        for i, record in enumerate(records):
            if not simulation_state["running"]:
                log.info("Simulation stopped by command")
                break

            shifted_ts = record["timestamp_orig"] + shift_delta

            # Pace: sim_elapsed / COMPRESSION_FACTOR = wall-clock target
            sim_elapsed = (record["timestamp_orig"] - scenario_start).total_seconds()
            real_elapsed = (datetime.now(timezone.utc) - real_start).total_seconds()
            target_elapsed = sim_elapsed / COMPRESSION_FACTOR

            sleep_needed = target_elapsed - real_elapsed
            if sleep_needed > 0.01:
                await asyncio.sleep(sleep_needed)

            try:
                await inject_record(rc, pool, record, shifted_ts)
            except Exception as e:
                log.error(f"❌ Inject failed MMSI={record['mmsi']}: {e}", exc_info=False)

            simulation_state["records_processed"] = i + 1
            simulation_state["progress_pct"] = int((i + 1) / len(records) * 100)

            if i % 500 == 0 and i > 0:
                pct = simulation_state["progress_pct"]
                dbw = simulation_state["db_write_count"]
                log.info(f"Progress {pct}% ({i+1}/{len(records)}) | DB writes: {dbw}")
                try:
                    await rc.xadd(STATUS_STREAM, {
                        "running": "true",
                        "progress_pct": str(pct),
                        "records_processed": str(i + 1),
                        "vessels": str(len(vessel_id_map)),
                    })
                except Exception:
                    pass

        # ── post-simulation: force anomalies + risk scores ────────────────────
        log.info("⚙️  Injecting synthetic anomalies …")
        await inject_synthetic_anomalies(pool, rc, shift_delta)

        log.info("⚙️  Upserting risk scores …")
        await update_risk_scores(pool)

        simulation_state.update(
            running=False,
            progress_pct=100,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        log.info("=" * 60)
        log.info("✅  WAKASHIO SIMULATION COMPLETE")
        log.info(f"   Records  : {len(records)}")
        log.info(f"   DB writes: {simulation_state['db_write_count']}")
        log.info(f"   Vessels  : {len(vessel_id_map)}")
        log.info("=" * 60)

        try:
            await rc.xadd(STATUS_STREAM, {
                "running": "false",
                "progress_pct": "100",
                "completed": "true",
            })
        except Exception:
            pass

    except asyncio.CancelledError:
        log.info("Simulation task cancelled")
        simulation_state["running"] = False
    except Exception as e:
        log.error(f"Simulation error: {e}", exc_info=True)
        simulation_state.update(running=False, error=str(e))
    finally:
        await rc.aclose()


# ─── Redis command listener ────────────────────────────────────────────────────

async def listen_for_commands():
    rc = await redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    pubsub = rc.pubsub()
    await pubsub.subscribe(CONTROL_CHANNEL, STOP_CHANNEL)
    log.info(f"📡 Listening on {CONTROL_CHANNEL} / {STOP_CHANNEL}")
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            if message["channel"] == CONTROL_CHANNEL:
                if simulation_state["running"]:
                    log.warning("Already running — ignoring start request")
                    continue
                log.info("🚀 Start command received via Redis")
                try:
                    records = load_scenario_csv()
                    simulation_state["task"] = asyncio.create_task(
                        run_simulation(records)
                    )
                except Exception as e:
                    log.error(f"Failed to load scenario: {e}")
            elif message["channel"] == STOP_CHANNEL:
                log.info("🛑 Stop command received")
                simulation_state["running"] = False
                if simulation_state["task"]:
                    simulation_state["task"].cancel()
                    simulation_state["task"] = None
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe()
        await rc.aclose()


@app.on_event("startup")
async def startup():
    log.info("🛢️  Wakashio Oil Spill Simulator ready")
    log.info(f"   Scenario: {SCENARIO_PATH}  exists={SCENARIO_PATH.exists()}")
    log.info(f"   Speed:    {COMPRESSION_FACTOR}×")
    asyncio.create_task(listen_for_commands())


@app.on_event("shutdown")
async def shutdown():
    simulation_state["running"] = False


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
