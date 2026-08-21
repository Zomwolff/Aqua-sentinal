"""
Response Decision worker - consumes spill.severity, generates response
recommendations, persists to response_recommendations, publishes to spill.response.
"""
from __future__ import annotations
import asyncio, logging, sys, time
from typing import Any, Dict, Optional
sys.path.insert(0, "/app")
from shared.db.connection import create_pool
from shared.redis_client import consume_stream, ensure_consumer_group, get_redis, publish_to_stream
from app.rules import generate_recommendations

log = logging.getLogger(__name__)
SERVICE_NAME   = "response-decision"
CONSUMER_GROUP = "response-decision"
CONSUMER_NAME  = "rd-worker"
INPUT_STREAM   = "spill.severity"
OUTPUT_STREAM  = "spill.response"

STATE: Dict[str, Any] = {
    "heartbeat": None, "spills_processed": 0,
    "recommendations_written": 0, "failed": 0, "last_spill_id": None,
}


def _float(val, default=0.0):
    try:
        return float(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


async def _fetch_attribution(pool, spill_id: str) -> Dict[str, Any]:
    row = await pool.fetchrow(
        """
        SELECT ar.final_score, v.mmsi, v.vessel_type
        FROM attribution_results ar
        JOIN vessels v ON v.id = ar.vessel_id
        WHERE ar.spill_id = $1
        ORDER BY ar.final_score DESC LIMIT 1
        """, spill_id,
    )
    if row:
        score = float(row["final_score"])
        label = ("probable_source" if score >= 0.75 else
                 "possible_source"  if score >= 0.50 else
                 "correlated"       if score >= 0.25 else "insufficient_evidence")
        return {"mmsi": str(row["mmsi"]), "vessel_type": str(row["vessel_type"] or "unknown"),
                "score": score, "label": label}
    return {"mmsi": "", "vessel_type": "unknown", "score": 0.0, "label": "insufficient_evidence"}


async def _fetch_3h_forecast(pool, spill_id: str):
    row = await pool.fetchrow(
        """
        SELECT ST_Y(ST_Centroid(geom)) AS lat, ST_X(ST_Centroid(geom)) AS lon
        FROM forecasts
        WHERE spill_id = $1
          AND horizon_hours BETWEEN 2.5 AND 3.5
        ORDER BY ABS(horizon_hours - 3.0) ASC LIMIT 1
        """, spill_id,
    )
    if row:
        return float(row["lat"]), float(row["lon"])
    return None, None


async def _process(data: Dict[str, Any], pool, redis) -> None:
    spill_id        = str(data.get("spill_id") or "")
    spill_lat       = _float(data.get("spill_lat"))
    spill_lon       = _float(data.get("spill_lon"))
    severity_level  = str(data.get("severity_level") or "LOW")
    severity_score  = _float(data.get("severity_score"))
    protected_risk  = _float(data.get("protected_area_risk"))
    population_risk = _float(data.get("population_risk"))
    protected_nearby= int(_float(data.get("protected_areas_nearby")))
    coast_dist      = _float(data.get("coast_distance_m"), default=20_000.0)
    is_synthetic    = str(data.get("is_synthetic", "")).lower() in ("true", "1")

    if not spill_id:
        log.warning("spill.severity missing spill_id")
        return

    attribution = await _fetch_attribution(pool, spill_id)
    fc_lat, fc_lon = await _fetch_3h_forecast(pool, spill_id)

    recs = generate_recommendations(
        spill_id=spill_id, spill_lat=spill_lat, spill_lon=spill_lon,
        severity_level=severity_level, severity_score=severity_score,
        protected_area_risk=protected_risk, population_risk=population_risk,
        protected_areas_nearby=protected_nearby, coast_distance_m=coast_dist,
        top_vessel_mmsi=attribution["mmsi"], top_vessel_type=attribution["vessel_type"],
        top_attribution_score=attribution["score"], top_attribution_label=attribution["label"],
        forecast_3h_lat=fc_lat, forecast_3h_lon=fc_lon, is_synthetic=is_synthetic,
    )

    for rec in recs:
        try:
            await pool.execute(
                """
                INSERT INTO response_recommendations
                    (spill_id, recommendation, priority, status, generated_at)
                VALUES ($1, $2, $3::recommendation_priority_enum, 'pending', NOW())
                """,
                spill_id, rec["recommendation"], rec["priority"],
            )
            STATE["recommendations_written"] += 1
        except Exception as e:
            log.error("response_recommendations insert failed spill=%s: %s", spill_id, e)

    await publish_to_stream(redis, OUTPUT_STREAM, {
        "spill_id": spill_id, "severity_level": severity_level,
        "recommendations_count": len(recs),
        "top_priority": recs[0]["priority"] if recs else "LOW",
        "is_synthetic": is_synthetic,
    })
    STATE["spills_processed"] += 1
    STATE["last_spill_id"] = spill_id
    log.info("Response: spill=%s tier=%s %d recommendations", spill_id, severity_level, len(recs))


async def run_response_worker() -> None:
    pool  = await create_pool()
    redis = await get_redis()
    await ensure_consumer_group(redis, INPUT_STREAM, CONSUMER_GROUP)
    log.info("%s worker started.", SERVICE_NAME)
    while True:
        STATE["heartbeat"] = time.time()
        messages = await consume_stream(
            redis, INPUT_STREAM, CONSUMER_GROUP, CONSUMER_NAME, count=10, block_ms=2000,
        )
        for msg in messages:
            try:
                await _process(msg["data"], pool, redis)
            except Exception as exc:
                STATE["failed"] += 1
                log.exception("Response decision failed msg=%s: %s", msg.get("id"), exc)
