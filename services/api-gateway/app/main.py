"""
Aqua-Sentinel API Gateway — unified backend for the dashboard and external consumers.

All endpoints below replace the need for direct Docker/DB/Redis CLI commands.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SYSTEM / MONITORING
  GET  /health                  — gateway liveness
  GET  /system/health           — aggregate health of ALL microservices
  GET  /system/pipeline         — Redis stream lengths + consumer lag
  GET  /system/stats            — ingestion counts, anomaly counts, etc.

VESSELS
  GET  /vessels                 — list tracked vessels (filters: type, flag, risk_tier)
  GET  /vessels/{mmsi}          — full vessel detail (pos + features + risk + anomalies)
  GET  /vessels/{mmsi}/track    — position history (last N hours)
  GET  /vessels/{mmsi}/risk     — risk score + factor breakdown
  GET  /vessels/{mmsi}/anomalies— anomaly history
  GET  /vessels/{mmsi}/trust    — trust score history
  GET  /vessels/risk/leaderboard— top N highest-risk vessels

ANOMALIES
  GET  /anomalies               — recent anomaly events (filters: severity, type, since)

STS EVENTS
  GET  /sts                     — completed STS encounters
  GET  /sts/active              — currently active encounters (from Redis)

SPOOFING
  GET  /spoofing/suspects       — vessels below trust threshold

RISK
  GET  /risk/vessels            — risk-ranked vessel list
  GET  /risk/tasking-requests   — satellite tasking requests

LIVE
  WS   /live                   — WebSocket: pushes risk alerts + anomaly events in real time
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set

import asyncpg
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, "/app")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("api-gateway")

# ── Internal service URLs (Docker service names) ──────────────────────────────
_SERVICES = {
    "data-ingestion":       "http://data-ingestion:8000",
    "ais-analytics":        "http://ais-analytics:8000",
    "anomaly-detection":    "http://anomaly-detection:8000",
    "ais-spoof-detection":  "http://ais-spoof-detection:8000",
    "sts-detection":        "http://sts-detection:8000",
    "vessel-risk-engine":   "http://vessel-risk-engine:8000",
    "source-attribution":   "http://source-attribution:8000",
    "drift-forecast":       "http://drift-forecast:8000",
    "severity-impact":      "http://severity-impact:8000",
    "response-decision":    "http://response-decision:8000",
}

# ── DB / Redis helpers (direct connections for aggregation) ───────────────────
_pool: Optional[asyncpg.Pool] = None
_redis: Optional[aioredis.Redis] = None

# ── WebSocket connection manager ──────────────────────────────────────────────
class _WSManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, data: dict):
        dead = set()
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.add(ws)
        self.active -= dead

ws_manager = _WSManager()


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=os.environ.get("POSTGRES_HOST", "postgres"),
            port=int(os.environ.get("POSTGRES_PORT", 5432)),
            user=os.environ.get("POSTGRES_USER", "postgres"),
            password=os.environ.get("POSTGRES_PASSWORD", "postgres"),
            database=os.environ.get("POSTGRES_DB", "maritime_oilspill"),
            min_size=2, max_size=10,
        )
    return _pool


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(
            f"redis://{os.environ.get('REDIS_HOST', 'redis')}:{os.environ.get('REDIS_PORT', 6379)}",
            decode_responses=True,
        )
    return _redis


# ── Background alert pusher ────────────────────────────────────────────────────
async def _alert_pusher():
    """Consume all event streams and push to WebSocket clients."""
    redis = await _get_redis()
    last_ids = {
        "ais.clean":                "$",
        "anomaly.events":           "$",
        "vessel.risk":              "$",
        "sts.events":               "$",
        "spill.candidates.filtered": "$",
        "incident.fused":           "$",
        "spill.attributed":         "$",
        "spill.severity":           "$",
        "spill.response":           "$",
        "sar.tasking.events":       "$",
        "dark.vessel.events":       "$",
        "ais.fetch.status":         "$",
    }
    # Map stream name → WS event type
    _TYPE_MAP = {
        "ais.clean":                 "ais",
        "anomaly.events":            "anomaly",
        "vessel.risk":               "risk",
        "sts.events":                "sts",
        "spill.candidates.filtered": "spill_candidate",
        "incident.fused":            "incident_fused",
        "spill.attributed":          "spill_attributed",
        "spill.severity":            "spill_severity",
        "spill.response":            "spill_response",
        "sar.tasking.events":        "sar_tasking",
        "dark.vessel.events":        "dark_vessel",
        "ais.fetch.status":          "ais_fetch",
    }
    log.info("Alert pusher started (watching %d streams).", len(last_ids))
    
    ais_throttle_counter = 0

    while True:
        try:
            for stream, last_id in list(last_ids.items()):
                results = await redis.xread({stream: last_id}, count=200, block=500)
                if not results:
                    continue
                for stream_name, messages in results:
                    for msg_id, data in messages:
                        last_ids[stream_name] = msg_id
                        
                        if stream_name == "ais.clean":
                            ais_throttle_counter += 1
                            # Sub-sample AIS messages to prevent overwhelming the browser
                            if ais_throttle_counter % 3 != 0:
                                continue

                        payload = {k: v for k, v in data.items()}
                        event_type = _TYPE_MAP.get(stream_name, stream_name)
                        await ws_manager.broadcast({
                            "type": event_type,
                            "stream": stream_name,
                            "id": msg_id,
                            "data": payload,
                            "at": datetime.now(timezone.utc).isoformat(),
                        })
        except Exception as e:
            log.error("Alert pusher error: %s", e)
            await asyncio.sleep(2)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await _get_pool()
    await _get_redis()
    pusher = asyncio.create_task(_alert_pusher())
    log.info("API Gateway started.")
    yield
    pusher.cancel()
    if _pool:
        await _pool.close()
    if _redis:
        await _redis.aclose()


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Aqua-Sentinel API Gateway",
    description="Unified API for vessel monitoring, anomaly detection, risk scoring, and pipeline observability.",
    version="1.0.0",
    lifespan=_lifespan,
)
# CORS: configurable via ALLOWED_ORIGINS (comma-separated). Default "*" keeps
# local development working; when a wildcard is used, credentials are disabled
# per the CORS spec (allow_credentials=True + "*" is an invalid combination).
_allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
_wildcard = "*" in _allowed_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=not _wildcard,
    allow_methods=["*"], allow_headers=["*"],
)

from fastapi.staticfiles import StaticFiles
import os
if os.path.exists("/data/artifacts"):
    app.mount("/artifacts", StaticFiles(directory="/data/artifacts"), name="artifacts")


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM / MONITORING
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["System"])
async def gateway_health():
    """Gateway liveness check."""
    return {"status": "ok", "service": "api-gateway", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/system/health", tags=["System"])
async def system_health():
    """
    Aggregate health of ALL microservices.
    Hits each service's /health endpoint concurrently and returns a unified status.
    """
    async def _check(name: str, base_url: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{base_url}/health")
                data = r.json()
                return {"service": name, "status": "ok", "url": base_url, **data}
        except Exception as e:
            return {"service": name, "status": "down", "url": base_url, "error": str(e)}

    results = await asyncio.gather(*[
        _check(name, url) for name, url in _SERVICES.items()
    ])

    all_ok = all(r["status"] == "ok" for r in results)
    return {
        "overall": "healthy" if all_ok else "degraded",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "services": results,
    }


@app.get("/system/pipeline", tags=["System"])
async def pipeline_status():
    """
    Redis stream lengths and consumer group lag per stream.
    Shows exactly how many messages are queued and how far behind each consumer is.
    """
    redis = await _get_redis()
    streams = ["ais.clean", "ais.features", "anomaly.events", "ais.trust", "sts.events", "vessel.risk"]
    groups_per_stream = {
        "ais.clean":       ["spoof-detection"],
        "ais.features":    ["anomaly-detection", "sts-detection"],
        "anomaly.events":  ["risk-engine"],
        "ais.trust":       ["risk-engine"],
        "sts.events":      ["risk-engine"],
    }

    result = []
    for stream in streams:
        try:
            length = await redis.xlen(stream)
            groups_info = []
            for group in groups_per_stream.get(stream, []):
                try:
                    info = await redis.xinfo_groups(stream)
                    for g in info:
                        if g["name"] == group:
                            groups_info.append({
                                "group": group,
                                "pending": g["pending"],
                                "consumers": g["consumers"],
                                "last_delivered_id": g["last-delivered-id"],
                            })
                except Exception:
                    pass
            result.append({
                "stream": stream,
                "length": length,
                "consumer_groups": groups_info,
            })
        except Exception as e:
            result.append({"stream": stream, "error": str(e)})

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "streams": result,
    }


@app.get("/system/stats", tags=["System"])
async def system_stats():
    """
    High-level pipeline statistics from the database.
    Returns counts for vessels, positions, anomalies, risk scores, STS events, etc.
    """
    pool = await _get_pool()
    rows = await pool.fetch("""
        SELECT
            (SELECT COUNT(*) FROM vessels)                                            AS total_vessels,
            (SELECT COUNT(*) FROM vessels WHERE last_seen >= NOW() - INTERVAL '1 hour')  AS active_vessels_1h,
            (SELECT COUNT(*) FROM vessels WHERE last_seen >= NOW() - INTERVAL '24 hours') AS active_vessels_24h,
            (SELECT COUNT(*) FROM vessel_positions WHERE timestamp >= NOW() - INTERVAL '1 hour') AS positions_last_1h,
            (SELECT COUNT(*) FROM vessel_positions WHERE timestamp >= NOW() - INTERVAL '24 hours') AS positions_last_24h,
            (SELECT COUNT(*) FROM anomaly_events WHERE window_start >= NOW() - INTERVAL '24 hours') AS anomalies_24h,
            (SELECT COUNT(*) FROM anomaly_events WHERE severity = 'HIGH' AND window_start >= NOW() - INTERVAL '24 hours') AS high_anomalies_24h,
            (SELECT COUNT(*) FROM ais_trust_scores WHERE flag = 'spoofing_suspected' AND timestamp >= NOW() - INTERVAL '24 hours') AS spoofing_flags_24h,
            (SELECT COUNT(*) FROM sts_events WHERE start_time >= NOW() - INTERVAL '24 hours') AS sts_events_24h,
            (SELECT COUNT(*) FROM vessel_risk_scores WHERE tier = 'HIGH')            AS high_risk_vessels,
            (SELECT COUNT(*) FROM vessel_risk_scores WHERE tier = 'CRITICAL')        AS critical_risk_vessels,
            (SELECT COUNT(*) FROM satellite_tasking_requests WHERE requested_at >= NOW() - INTERVAL '24 hours') AS tasking_requests_24h
    """)
    stats = dict(rows[0]) if rows else {}
    return {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        **{k: int(v) if v is not None else 0 for k, v in stats.items()},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# VESSELS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/vessels", tags=["Vessels"])
async def list_vessels(
    vessel_type: Optional[str] = Query(None, description="Filter by vessel type (tanker, cargo, fishing…)"),
    risk_tier:   Optional[str] = Query(None, description="Filter by risk tier: LOW | MEDIUM | HIGH | CRITICAL"),
    active_since_hours: int    = Query(24, description="Only include vessels seen in the last N hours"),
    limit: int = Query(200, le=1000),
    offset: int = Query(0, ge=0),
):
    """
    List all tracked vessels with their last known position, type, and current risk tier.
    This is the primary feed for the map view.
    """
    pool = await _get_pool()
    conditions = ["v.last_seen >= NOW() - ($1 || ' hours')::INTERVAL"]
    params: list = [str(active_since_hours)]
    idx = 2

    if vessel_type:
        conditions.append(f"v.vessel_type = ${idx}"); params.append(vessel_type); idx += 1
    if risk_tier:
        conditions.append(f"r.tier = ${idx}"); params.append(risk_tier); idx += 1

    where = "WHERE " + " AND ".join(conditions)
    params += [limit, offset]

    rows = await pool.fetch(f"""
        SELECT
            v.mmsi, v.name AS vessel_name, v.vessel_type, v.flag, v.imo_number,
            v.last_lat, v.last_lon, v.last_seen,
            v.destination, v.draught,
            r.risk_score, r.tier AS risk_tier, r.recommended_action,
            sat.status AS sar_status,
            attr.spill_id AS detected_spill_id
        FROM vessels v
        LEFT JOIN vessel_risk_scores r ON v.mmsi = r.mmsi
        LEFT JOIN (
            SELECT DISTINCT ON (vessel_id) vessel_id, status
            FROM satellite_tasking_requests
            ORDER BY vessel_id, requested_at DESC
        ) sat ON sat.vessel_id = v.id
        LEFT JOIN (
            SELECT DISTINCT ON (vessel_id) vessel_id, spill_id
            FROM attribution_results
            ORDER BY vessel_id, computed_at DESC
        ) attr ON attr.vessel_id = v.id
        {where}
        ORDER BY v.last_seen DESC
        LIMIT ${idx} OFFSET ${idx+1}
    """, *params)

    return {
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "vessels": [
            {
                **{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
            }
            for r in rows
        ],
    }


@app.get("/vessels/{mmsi}", tags=["Vessels"])
async def get_vessel_detail(mmsi: int):
    """
    Full detail for a single vessel: static info, last position, latest
    behavioral features, current risk score, and recent anomalies.
    """
    pool = await _get_pool()

    vessel_row = await pool.fetchrow("SELECT * FROM vessels WHERE mmsi=$1", str(mmsi))
    if not vessel_row:
        raise HTTPException(status_code=404, detail=f"Vessel MMSI {mmsi} not found")

    vessel = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(vessel_row).items()}

    # Latest features
    feat_row = await pool.fetchrow(
        "SELECT * FROM vessel_features WHERE mmsi=$1 ORDER BY window_end DESC LIMIT 1", str(mmsi)
    )
    features = None
    if feat_row:
        features = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(feat_row).items()}
        if isinstance(features.get("proximity_events"), str):
            try:
                features["proximity_events"] = json.loads(features["proximity_events"])
            except Exception:
                pass

    # Risk score
    risk_row = await pool.fetchrow("SELECT * FROM vessel_risk_scores WHERE mmsi=$1", str(mmsi))
    risk = None
    if risk_row:
        risk = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(risk_row).items()}
        if isinstance(risk.get("contributing_factors"), str):
            try:
                risk["contributing_factors"] = json.loads(risk["contributing_factors"])
            except Exception:
                pass

    # Recent anomalies (last 24h)
    anomaly_rows = await pool.fetch(
        "SELECT id, anomaly_type, severity, window_start, evidence FROM anomaly_events "
        "WHERE mmsi=$1 AND window_start >= NOW() - INTERVAL '24 hours' ORDER BY window_start DESC LIMIT 20",
        str(mmsi),
    )
    anomalies = []
    for r in anomaly_rows:
        row = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
        if isinstance(row.get("evidence"), str):
            try:
                row["evidence"] = json.loads(row["evidence"])
            except Exception:
                pass
        anomalies.append(row)

    # Trust score
    trust_row = await pool.fetchrow(
        "SELECT rolling_trust_score, flag, timestamp FROM ais_trust_scores "
        "WHERE mmsi=$1 ORDER BY timestamp DESC LIMIT 1", str(mmsi)
    )
    trust = dict(trust_row) if trust_row else None
    if trust and trust.get("timestamp"):
        trust["timestamp"] = trust["timestamp"].isoformat()

    # SAR Tasking Request & Verdict (incl. the reasoning captured at task time)
    sar_tasking_row = await pool.fetchrow(
        "SELECT id, status, requested_at, scene_id, completed_at, risk_score, risk_tier, reason "
        "FROM satellite_tasking_requests "
        "WHERE mmsi=$1 ORDER BY requested_at DESC LIMIT 1", str(mmsi)
    )
    sar_tasking = None
    if sar_tasking_row:
        sar_tasking = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(sar_tasking_row).items()}

    # Determine Verdict
    verdict = {"status": "none", "spill_id": None}
    if sar_tasking_row:
        if sar_tasking_row["status"] == "pending":
            verdict["status"] = "pending"
        elif sar_tasking_row["status"] == "fulfilled":
            # Check if this vessel has a spill attributed to it
            attr_row = await pool.fetchrow(
                "SELECT ar.spill_id FROM attribution_results ar "
                "JOIN vessels v ON v.id = ar.vessel_id "
                "WHERE v.mmsi=$1 ORDER BY ar.computed_at DESC LIMIT 1", str(mmsi)
            )
            if attr_row:
                verdict["status"] = "spill_detected"
                verdict["spill_id"] = str(attr_row["spill_id"])
            else:
                verdict["status"] = "no_spill_detected"

    return {
        "vessel": vessel,
        "features": features,
        "risk": risk,
        "anomalies": anomalies,
        "trust": trust,
        "sar_tasking": sar_tasking,
        "verdict": verdict,
    }


@app.get("/vessels/{mmsi}/track", tags=["Vessels"])
async def get_vessel_track(
    mmsi: int,
    hours: int = Query(6, ge=1, le=168, description="Number of hours of track history"),
    limit: int = Query(500, le=2000),
):
    """
    Position history (track) for a vessel. Returns lat/lon/timestamp/speed/course
    suitable for drawing a path on a map.
    """
    pool = await _get_pool()
    rows = await pool.fetch(
        """SELECT p.timestamp, p.latitude AS lat, p.longitude AS lon,
                  p.speed_knots, p.course_deg AS course, p.heading_deg AS heading,
                  p.nav_status
           FROM vessel_positions p
           JOIN vessels v ON p.vessel_id = v.id
           WHERE v.mmsi=$1 AND p.timestamp >= NOW() - ($2 || ' hours')::INTERVAL
           ORDER BY p.timestamp ASC
           LIMIT $3""",
        str(mmsi), str(hours), limit,
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"No track data for MMSI {mmsi} in last {hours}h")

    return {
        "mmsi": mmsi,
        "hours": hours,
        "point_count": len(rows),
        "track": [
            {
                "timestamp": r["timestamp"].isoformat(),
                "lat": r["lat"], "lon": r["lon"],
                "speed_knots": r["speed_knots"],
                "course": r["course"], "heading": r["heading"],
                "nav_status": r["nav_status"],
            }
            for r in rows
        ],
    }


@app.get("/vessels/{mmsi}/anomalies", tags=["Vessels"])
async def get_vessel_anomalies(
    mmsi: int,
    hours: int = Query(48, ge=1, le=720),
    limit: int = Query(100, le=500),
):
    """Anomaly history for a specific vessel."""
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT id, anomaly_type, severity, window_start, evidence, source "
        "FROM anomaly_events WHERE mmsi=$1 AND window_start >= NOW() - ($2 || ' hours')::INTERVAL "
        "ORDER BY window_start DESC LIMIT $3",
        str(mmsi), str(hours), limit,
    )
    result = []
    for r in rows:
        row = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
        if isinstance(row.get("evidence"), str):
            try:
                row["evidence"] = json.loads(row["evidence"])
            except Exception:
                pass
        result.append(row)
    return {"mmsi": mmsi, "hours": hours, "count": len(result), "anomalies": result}


@app.get("/vessels/{mmsi}/trust", tags=["Vessels"])
async def get_vessel_trust(mmsi: int, limit: int = Query(50, le=200)):
    """Trust score history for a specific vessel."""
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT timestamp, instant_trust_score, rolling_trust_score, "
        "speed_jump_flag, identity_change_flag, mmsi_validity_flag, flag "
        "FROM ais_trust_scores WHERE mmsi=$1 ORDER BY timestamp DESC LIMIT $2",
        str(mmsi), limit,
    )
    return {
        "mmsi": mmsi,
        "history": [
            {
                "timestamp": r["timestamp"].isoformat(),
                "instant": r["instant_trust_score"],
                "rolling": r["rolling_trust_score"],
                "speed_jump": r["speed_jump_flag"],
                "identity_change": r["identity_change_flag"],
                "mmsi_valid": r["mmsi_validity_flag"],
                "flag": r["flag"],
            }
            for r in rows
        ],
    }


@app.get("/vessels/{mmsi}/risk", tags=["Vessels"])
async def get_vessel_risk(mmsi: int):
    """Current risk score with full contributing factor breakdown for a vessel."""
    pool = await _get_pool()
    row = await pool.fetchrow("SELECT * FROM vessel_risk_scores WHERE mmsi=$1", str(mmsi))
    if not row:
        raise HTTPException(status_code=404, detail=f"No risk score for MMSI {mmsi}")
    result = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(row).items()}
    if isinstance(result.get("contributing_factors"), str):
        try:
            result["contributing_factors"] = json.loads(result["contributing_factors"])
        except Exception:
            pass
    return result


@app.get("/vessels/risk/leaderboard", tags=["Vessels"])
async def risk_leaderboard(
    tier: Optional[str] = Query(None, description="Filter: LOW | MEDIUM | HIGH | CRITICAL"),
    limit: int = Query(50, le=200),
):
    """Top vessels by risk score — the high-risk watchlist."""
    pool = await _get_pool()
    conditions = []
    params: list = []
    idx = 1
    if tier:
        conditions.append(f"r.tier = ${idx}"); params.append(tier); idx += 1
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)

    rows = await pool.fetch(f"""
        SELECT
            r.mmsi, v.name AS vessel_name, v.vessel_type, v.flag,
            v.last_lat, v.last_lon, v.last_seen,
            r.risk_score, r.tier, r.recommended_action, r.updated_at
        FROM vessel_risk_scores r
        JOIN vessels v ON r.mmsi = v.mmsi
        {where}
        ORDER BY r.risk_score DESC
        LIMIT ${idx}
    """, *params)

    return {
        "count": len(rows),
        "vessels": [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
            for r in rows
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ANOMALIES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/anomalies", tags=["Anomalies"])
async def list_anomalies(
    severity:     Optional[str] = Query(None, description="LOW | MEDIUM | HIGH"),
    anomaly_type: Optional[str] = Query(None, description="e.g. sudden_stop, erratic_course, ais_gap"),
    mmsi:         Optional[int] = Query(None),
    since_hours:  int           = Query(24, ge=1, le=720),
    limit:        int           = Query(100, le=1000),
):
    """
    Recent anomaly events from the detection pipeline.
    Each event includes the type, severity, and a full evidence dict explaining why it was flagged.
    """
    pool = await _get_pool()
    conditions = ["a.window_start >= NOW() - ($1 || ' hours')::INTERVAL"]
    params: list = [str(since_hours)]
    idx = 2

    if severity:
        conditions.append(f"a.severity = ${idx}"); params.append(severity); idx += 1
    if anomaly_type:
        conditions.append(f"a.anomaly_type = ${idx}"); params.append(anomaly_type); idx += 1
    if mmsi:
        conditions.append(f"a.mmsi = ${idx}"); params.append(str(mmsi)); idx += 1

    params.append(limit)
    rows = await pool.fetch(f"""
        SELECT
            a.id, a.mmsi, v.name AS vessel_name, v.vessel_type,
            a.anomaly_type, a.severity, a.window_start, a.evidence, a.source
        FROM anomaly_events a
        LEFT JOIN vessels v ON a.mmsi = v.mmsi
        WHERE {" AND ".join(conditions)}
        ORDER BY a.window_start DESC
        LIMIT ${idx}
    """, *params)

    result = []
    for r in rows:
        row = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
        if isinstance(row.get("evidence"), str):
            try:
                row["evidence"] = json.loads(row["evidence"])
            except Exception:
                pass
        result.append(row)

    return {"count": len(result), "since_hours": since_hours, "anomalies": result}


# ═══════════════════════════════════════════════════════════════════════════════
# STS EVENTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/sts", tags=["STS"])
async def list_sts_events(
    mmsi:        Optional[int] = Query(None, description="Filter by either vessel in the pair"),
    since_hours: int = Query(48, ge=1, le=720),
    limit:       int = Query(50, le=500),
):
    """
    Ship-to-Ship transfer encounters. High-confidence STS events between tankers
    are a primary indicator of illegal oil transfer.
    """
    pool = await _get_pool()
    conditions = ["s.start_time >= NOW() - ($1 || ' hours')::INTERVAL"]
    params: list = [str(since_hours)]
    idx = 2

    if mmsi:
        conditions.append(f"(s.vessel_a_mmsi = ${idx} OR s.vessel_b_mmsi = ${idx})")
        params.append(str(mmsi)); idx += 1

    params.append(limit)
    rows = await pool.fetch(f"""
        SELECT
            s.id, s.vessel_a_mmsi AS vessel_a, va.name AS vessel_a_name, va.vessel_type AS vessel_a_type,
            s.vessel_b_mmsi AS vessel_b, vb.name AS vessel_b_name, vb.vessel_type AS vessel_b_type,
            s.start_time, s.end_time, s.duration_minutes,
            s.avg_distance_m, s.min_distance_m, s.avg_combined_speed_knots, s.confidence,
            ST_Y(s.location::geometry) AS centroid_lat,
            ST_X(s.location::geometry) AS centroid_lon
        FROM sts_events s
        LEFT JOIN vessels va ON s.vessel_a_mmsi = va.mmsi
        LEFT JOIN vessels vb ON s.vessel_b_mmsi = vb.mmsi
        WHERE {" AND ".join(conditions)}
        ORDER BY s.start_time DESC
        LIMIT ${idx}
    """, *params)

    return {
        "count": len(rows),
        "since_hours": since_hours,
        "events": [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
            for r in rows
        ],
    }


@app.get("/sts/active", tags=["STS"])
async def list_active_sts():
    """Currently active STS encounters (live, from Redis — not yet in DB)."""
    redis = await _get_redis()
    keys = await redis.keys("sts:active:*")
    active = []
    now = datetime.now(timezone.utc).timestamp()
    for key in keys:
        raw = await redis.hgetall(key)
        if not raw:
            continue
        parts = key.split(":")
        if len(parts) >= 4:
            start_epoch = float(raw.get("start_time", now))
            duration_s = now - start_epoch
            active.append({
                "vessel_a": int(parts[2]),
                "vessel_b": int(parts[3]),
                "start_time": datetime.fromtimestamp(start_epoch, tz=timezone.utc).isoformat(),
                "duration_minutes": round(duration_s / 60, 1),
                "sample_count": int(raw.get("sample_count", 0)),
                "min_distance_m": float(raw.get("min_distance_m", 0)),
                "emitted": raw.get("emitted") == "true",
            })
    return {"count": len(active), "encounters": sorted(active, key=lambda x: x["duration_minutes"], reverse=True)}


# ═══════════════════════════════════════════════════════════════════════════════
# SPOOFING
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/spoofing/suspects", tags=["Spoofing"])
async def spoofing_suspects(
    trust_threshold: float = Query(0.5, ge=0.0, le=1.0, description="Vessels below this score are flagged"),
    limit: int = Query(50, le=200),
):
    """Vessels currently below the AIS trust threshold — potential spoofing suspects."""
    pool = await _get_pool()
    rows = await pool.fetch("""
        SELECT DISTINCT ON (t.mmsi)
            t.mmsi, v.name AS vessel_name, v.vessel_type, v.flag,
            v.last_lat, v.last_lon, v.last_seen,
            t.rolling_trust_score, t.instant_trust_score,
            t.speed_jump_flag, t.identity_change_flag, t.mmsi_validity_flag,
            t.flag AS trust_flag, t.timestamp
        FROM ais_trust_scores t
        LEFT JOIN vessels v ON t.mmsi = v.mmsi
        WHERE t.rolling_trust_score < $1
        ORDER BY t.mmsi, t.timestamp DESC
        LIMIT $2
    """, trust_threshold, limit)

    return {
        "trust_threshold": trust_threshold,
        "count": len(rows),
        "suspects": [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
            for r in rows
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# RISK
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/risk/vessels", tags=["Risk"])
async def risk_vessels(
    tier: Optional[str] = Query(None),
    min_score: Optional[float] = Query(None, ge=0, le=100),
    limit: int = Query(100, le=500),
):
    """All vessels with their current risk scores, sorted by risk descending."""
    pool = await _get_pool()
    conditions = []
    params: list = []
    idx = 1
    if tier:
        conditions.append(f"r.tier = ${idx}"); params.append(tier); idx += 1
    if min_score is not None:
        conditions.append(f"r.risk_score >= ${idx}"); params.append(min_score); idx += 1
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)

    rows = await pool.fetch(f"""
        SELECT
            r.mmsi, v.name AS vessel_name, v.vessel_type, v.flag,
            v.last_lat, v.last_lon, v.last_seen, v.destination,
            r.risk_score, r.tier, r.contributing_factors,
            r.recommended_action, r.updated_at
        FROM vessel_risk_scores r
        JOIN vessels v ON r.mmsi = v.mmsi
        {where}
        ORDER BY r.risk_score DESC
        LIMIT ${idx}
    """, *params)

    result = []
    for r in rows:
        row = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
        if isinstance(row.get("contributing_factors"), str):
            try:
                row["contributing_factors"] = json.loads(row["contributing_factors"])
            except Exception:
                pass
        result.append(row)

    return {"count": len(result), "vessels": result}


@app.get("/risk/tasking-requests", tags=["Risk"])
async def tasking_requests(
    status: Optional[str] = Query(None, description="pending | dispatched | completed"),
    limit: int = Query(50, le=200),
):
    """Satellite tasking requests raised for HIGH/CRITICAL vessels."""
    pool = await _get_pool()
    conditions = []
    params: list = []
    idx = 1
    if status:
        conditions.append(f"t.status = ${idx}"); params.append(status); idx += 1
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)

    rows = await pool.fetch(f"""
        SELECT
            t.id, t.mmsi, v.name AS vessel_name, v.vessel_type,
            v.last_lat, v.last_lon,
            t.risk_score, t.risk_tier, t.reason,
            t.requested_at, t.status
        FROM satellite_tasking_requests t
        LEFT JOIN vessels v ON t.mmsi = v.mmsi
        {where}
        ORDER BY t.requested_at DESC
        LIMIT ${idx}
    """, *params)

    result = []
    for r in rows:
        row = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
        if isinstance(row.get("reason"), str):
            try:
                row["reason"] = json.loads(row["reason"])
            except Exception:
                pass
        result.append(row)

    return {"count": len(result), "requests": result}


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURES (raw analytics output)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/features", tags=["Analytics"])
async def list_features(
    mmsi: Optional[int] = Query(None),
    since_hours: int = Query(6, ge=1, le=168),
    limit: int = Query(100, le=500),
):
    """
    Raw behavioral feature windows computed by the analytics service.
    Each row is a 15-minute window with speed stats, course variance, loitering score, etc.
    """
    pool = await _get_pool()
    conditions = ["window_end >= NOW() - ($1 || ' hours')::INTERVAL"]
    params: list = [str(since_hours)]
    idx = 2
    if mmsi:
        conditions.append(f"mmsi = ${idx}"); params.append(str(mmsi)); idx += 1
    params.append(limit)

    rows = await pool.fetch(
        f"SELECT * FROM vessel_features WHERE {' AND '.join(conditions)} "
        f"ORDER BY window_end DESC LIMIT ${idx}",
        *params,
    )
    result = []
    for r in rows:
        row = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
        if isinstance(row.get("proximity_events"), str):
            try:
                row["proximity_events"] = json.loads(row["proximity_events"])
            except Exception:
                pass
        result.append(row)
    return {"count": len(result), "features": result}


# ═══════════════════════════════════════════════════════════════════════════════
# SPILL CANDIDATES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/spill/candidates/{candidate_id}")
async def get_spill_candidate(candidate_id: str):
    """
    Full details for one spill incident by its ID (used by SpillCandidateLayer).
    Falls back to spill_incidents table (spill_candidates table no longer used).
    Returns GeoJSON geometry derived from the PostGIS centroid point.
    """
    pool = await _get_pool()
    row = await pool.fetchrow(
        """
        SELECT
            id AS candidate_id,
            source_image_id AS scene_id,
            detected_at AS acquisition_time,
            status AS classification_label,
            confidence,
            area_km2 * 1000000.0 AS area_m2,
            source AS raw_source,
            ST_AsGeoJSON(geom) AS geometry
        FROM spill_incidents
        WHERE id = $1
        """,
        candidate_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="candidate not found")

    result = {k: v for k, v in dict(row).items()}
    if isinstance(result.get("geometry"), str):
        result["geometry"] = json.loads(result["geometry"])
    if isinstance(result.get("acquisition_time"), datetime):
        result["acquisition_time"] = result["acquisition_time"].isoformat()
    if result.get("confidence") is not None:
        result["confidence"] = float(result["confidence"])
    if result.get("area_m2") is not None:
        result["area_m2"] = float(result["area_m2"])
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SPILL INCIDENTS (Module B)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/spill/incidents", tags=["SpillIntelligence"])
async def list_spill_incidents(
    status: Optional[str] = Query(None, description="detected | attributed | responded"),
    since_hours: int = Query(72, ge=1, le=720),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    """
    All spill incidents with their current severity, top attributed vessel, and
    latest response status. Primary feed for the dashboard Incident List panel.
    """
    pool = await _get_pool()
    conditions = ["si.detected_at >= NOW() - ($1 || ' hours')::INTERVAL"]
    params: list = [str(since_hours)]
    idx = 2
    if status:
        conditions.append(f"si.status = ${idx}"); params.append(status); idx += 1
    params += [limit, offset]

    rows = await pool.fetch(f"""
        SELECT
            si.id,
            si.detected_at,
            si.latitude,
            si.longitude,
            si.area_km2,
            si.confidence,
            si.status,
            si.source,
            sev.severity_level,
            sev.score AS severity_score,
            sev.protected_area_risk,
            sev.population_risk,
            ar_top.final_score AS top_attribution_score,
            v_top.mmsi AS top_vessel_mmsi,
            v_top.vessel_type AS top_vessel_type
        FROM spill_incidents si
        LEFT JOIN severity sev ON sev.spill_id = si.id
        LEFT JOIN LATERAL (
            SELECT vessel_id, final_score FROM attribution_results
            WHERE spill_id = si.id ORDER BY final_score DESC LIMIT 1
        ) ar_top ON TRUE
        LEFT JOIN vessels v_top ON v_top.id = ar_top.vessel_id
        WHERE {" AND ".join(conditions)}
        ORDER BY si.detected_at DESC
        LIMIT ${idx} OFFSET ${idx+1}
    """, *params)

    return {
        "count": len(rows),
        "since_hours": since_hours,
        "incidents": [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
            for r in rows
        ],
    }


@app.get("/spill/incidents/{spill_id}", tags=["SpillIntelligence"])
async def get_spill_incident(spill_id: str):
    """Full detail for a spill incident: severity, attribution, forecast, and recommendations."""
    pool = await _get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, detected_at, latitude, longitude,
               ST_AsGeoJSON(geom) AS geometry,
               area_km2, confidence, status, source, source_image_id
        FROM spill_incidents WHERE id = $1
        """,
        spill_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Spill incident not found")

    incident = {k: v for k, v in dict(row).items()}
    if isinstance(incident.get("geometry"), str):
        incident["geometry"] = json.loads(incident["geometry"])
    if isinstance(incident.get("detected_at"), datetime):
        incident["detected_at"] = incident["detected_at"].isoformat()

    # Severity
    sev_row = await pool.fetchrow(
        "SELECT * FROM severity WHERE spill_id = $1", spill_id
    )
    severity = None
    if sev_row:
        severity = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(sev_row).items()}

    # Attribution top-5
    attr_rows = await pool.fetch(
        """
        SELECT ar.final_score, ar.distance_score, ar.trajectory_score,
               ar.wind_score, ar.time_score, ar.behavior_score, ar.model_version,
               v.mmsi, v.name AS vessel_name, v.vessel_type, v.flag,
               v.last_lat, v.last_lon
        FROM attribution_results ar
        JOIN vessels v ON v.id = ar.vessel_id
        WHERE ar.spill_id = $1
        ORDER BY ar.final_score DESC LIMIT 5
        """,
        spill_id,
    )
    attribution = [
        {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
        for r in attr_rows
    ]

    # Forecast polygons (incl. model confidence + version for the UI)
    forecast_rows = await pool.fetch(
        """
        SELECT id, horizon_hours,
               ST_AsGeoJSON(geom) AS geometry, generated_at,
               confidence, model_version
        FROM forecasts WHERE spill_id = $1 ORDER BY horizon_hours ASC
        """,
        spill_id,
    )
    forecasts = []
    for r in forecast_rows:
        fr = {k: v for k, v in dict(r).items()}
        if isinstance(fr.get("geometry"), str):
            fr["geometry"] = json.loads(fr["geometry"])
        if isinstance(fr.get("generated_at"), datetime):
            fr["generated_at"] = fr["generated_at"].isoformat()
        forecasts.append(fr)

    # Recommendations
    rec_rows = await pool.fetch(
        """
        SELECT id, recommendation, priority, status, generated_at,
               acknowledged_at, acknowledged_by
        FROM response_recommendations
        WHERE spill_id = $1
        ORDER BY
            CASE priority WHEN 'URGENT' THEN 1 WHEN 'HIGH' THEN 2
                          WHEN 'MEDIUM' THEN 3 ELSE 4 END,
            generated_at ASC
        """,
        spill_id,
    )
    recommendations = [
        {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
        for r in rec_rows
    ]

    return {
        "incident": incident,
        "severity": severity,
        "attribution": attribution,
        "forecasts": forecasts,
        "recommendations": recommendations,
    }


@app.get("/spill/incidents/{spill_id}/attribution", tags=["SpillIntelligence"])
async def get_spill_attribution(spill_id: str):
    """Source attribution results for a spill — which vessels were nearby and scored."""
    pool = await _get_pool()
    rows = await pool.fetch(
        """
        SELECT ar.final_score, ar.distance_score, ar.trajectory_score,
               ar.wind_score, ar.time_score, ar.behavior_score, ar.model_version,
               v.mmsi, v.name AS vessel_name, v.vessel_type, v.flag,
               v.last_lat, v.last_lon
        FROM attribution_results ar
        JOIN vessels v ON v.id = ar.vessel_id
        WHERE ar.spill_id = $1
        ORDER BY ar.final_score DESC
        """,
        spill_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No attribution data for this spill")
    return {
        "spill_id": spill_id,
        "candidates": [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
            for r in rows
        ],
    }


@app.get("/spill/incidents/{spill_id}/forecast", tags=["SpillIntelligence"])
async def get_spill_forecast(spill_id: str):
    """Lagrangian drift forecast polygons for a spill at 3h, 6h, 12h, 24h horizons."""
    pool = await _get_pool()
    rows = await pool.fetch(
        """
        SELECT id, horizon_hours,
               ST_AsGeoJSON(geom) AS geometry, generated_at
        FROM forecasts WHERE spill_id = $1
        ORDER BY horizon_hours ASC
        """,
        spill_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No forecast data for this spill")
    result = []
    for r in rows:
        fr = {k: v for k, v in dict(r).items()}
        if isinstance(fr.get("geometry"), str):
            fr["geometry"] = json.loads(fr["geometry"])
        if isinstance(fr.get("generated_at"), datetime):
            fr["generated_at"] = fr["generated_at"].isoformat()
        result.append(fr)
    return {"spill_id": spill_id, "horizons": result}


@app.get("/spill/incidents/{spill_id}/severity", tags=["SpillIntelligence"])
async def get_spill_severity(spill_id: str):
    """Severity scorecard for a spill (area, protected area, population risk)."""
    pool = await _get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM severity WHERE spill_id = $1", spill_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="No severity data for this spill")
    result = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(row).items()}
    return result


@app.get("/spill/incidents/{spill_id}/recommendations", tags=["SpillIntelligence"])
async def get_spill_recommendations(spill_id: str):
    """Response recommendations for a spill, ordered by priority."""
    pool = await _get_pool()
    rows = await pool.fetch(
        """
        SELECT id, recommendation, priority, status, generated_at,
               acknowledged_at, acknowledged_by
        FROM response_recommendations
        WHERE spill_id = $1
        ORDER BY
            CASE priority WHEN 'URGENT' THEN 1 WHEN 'HIGH' THEN 2
                          WHEN 'MEDIUM' THEN 3 ELSE 4 END,
            generated_at ASC
        """,
        spill_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No recommendations for this spill")
    return {
        "spill_id": spill_id,
        "recommendations": [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
            for r in rows
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE DATA TRIGGER + REFERENCE / DARK-VESSEL ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/ingest/ais/fetch-now", tags=["Ingestion"])
async def trigger_live_ais_fetch():
    """
    Request an immediate live-AIS poll from the ais-reader service.

    The request is published on the `control.fetch_ais` Redis channel; the
    reader interrupts its polling sleep, fetches from the configured provider
    (VesselAPI / AISStream), and the resulting positions flow through the
    normal pipeline — appearing in this gateway's /live WebSocket feed as
    `ais` events plus a summary `ais_fetch` event.
    """
    redis = await _get_redis()
    await redis.publish("control.fetch_ais", json.dumps({
        "requested_by": "ui",
        "at": datetime.now(timezone.utc).isoformat(),
    }))
    # Immediate UI feedback: the fetch has been requested.
    await ws_manager.broadcast({
        "type": "system",
        "stream": "control",
        "data": {"event": "ais_fetch_requested", "detail": "Manual live-AIS fetch requested"},
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "status": "accepted",
        "detail": "Live AIS fetch requested. Positions will appear in the live feed shortly.",
        "at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/protected-areas", tags=["Reference"])
async def list_protected_areas():
    """Protected maritime zones (marine protected areas, mangroves, ports…)
    rendered as map layers and used by the severity engine."""
    pool = await _get_pool()
    rows = await pool.fetch("""
        SELECT id, name, area_type AS type,
               ST_AsGeoJSON(geom) AS geometry
        FROM protected_areas
        ORDER BY name ASC
    """)
    features = []
    for r in rows:
        rec = dict(r)
        geometry = None
        if rec.get("geometry"):
            try:
                geometry = json.loads(rec["geometry"])
            except (TypeError, ValueError):
                geometry = None
        features.append({
            "id": str(rec["id"]),
            "name": rec.get("name"),
            "type": rec.get("type"),
            "geometry": geometry,
        })
    return {"count": len(features), "protected_areas": features}


@app.get("/dark-vessels", tags=["DarkVessel"])
async def list_dark_vessels(
    since_hours: int = Query(24, ge=1, le=720),
    limit: int = Query(200, le=1000),
):
    """Recent dark-vessel detections (AIS-gap analysis + SAR correlation)."""
    pool = await _get_pool()
    rows = await pool.fetch(
        """
        SELECT dve.id, dve.detected_at, dve.latitude, dve.longitude,
               dve.image_source, dve.sensor, dve.confidence,
               dve.length_est_m, dve.matched_mmsi,
               v.name AS matched_vessel_name
        FROM dark_vessel_events dve
        LEFT JOIN vessels v ON v.id = dve.matched_vessel_id
        WHERE dve.detected_at >= NOW() - ($1 || ' hours')::INTERVAL
        ORDER BY dve.detected_at DESC
        LIMIT $2
        """,
        str(since_hours), limit,
    )
    result = []
    for r in rows:
        rec = dict(r)
        rec["id"] = str(rec["id"])
        if isinstance(rec.get("detected_at"), datetime):
            rec["detected_at"] = rec["detected_at"].isoformat()
        for key in ("confidence", "length_est_m"):
            if rec.get(key) is not None:
                rec[key] = float(rec[key])
        result.append(rec)
    return {"count": len(result), "dark_vessels": result}


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/live")
async def live_ws(ws: WebSocket):
    """
    Real-time WebSocket feed. Pushes:
      - type: "anomaly"  — new anomaly event detected
      - type: "risk"     — vessel risk tier changed
      - type: "sts"      — STS encounter detected/updated
      - type: "heartbeat"— keep-alive every 10s
    """
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.send_json({
                "type": "heartbeat",
                "connected_clients": len(ws_manager.active),
                "at": datetime.now(timezone.utc).isoformat(),
            })
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception:
        ws_manager.disconnect(ws)
