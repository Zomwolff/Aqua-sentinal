"""
Source Attribution worker — consumes incident.fused, scores vessels,
creates spill_incidents, writes attribution_results.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/app")
from shared.db.connection import create_pool, get_pool, close_pool
from shared.redis_client import (
    close_redis, consume_stream, ensure_consumer_group,
    get_redis, publish_to_stream,
)
from app.attribution import (
    ATTRIBUTION_SPATIAL_WINDOW_M, ATTRIBUTION_TEMPORAL_WINDOW_H,
    MODEL_VERSION,
    attribution_label, compute_attribution_score,
    score_behavior, score_distance, score_time,
    score_trajectory, score_wind_drift, score_origin_proximity
)
from app.culprit_finder import find_origin_probability

log = logging.getLogger(__name__)

SERVICE_NAME   = "source-attribution"
CONSUMER_GROUP = "source-attribution"
CONSUMER_NAME  = "sa-worker"
INPUT_STREAM   = "incident.fused"
OUTPUT_STREAM  = "spill.attributed"

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "incidents_processed": 0,
    "attributions_written": 0,
    "failed": 0,
    "last_candidate_id": None,
    "last_spill_id": None,
}


def _parse_dt(raw: Any) -> Optional[datetime]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def _fetch_candidate_geometry(
    pool, candidate_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Resolve the real spill-candidate geometry from PostGIS.

    Returns dict(centroid_lat, centroid_lon, area_km2, geom_geojson) or None.
    This is the fallback for incident.fused messages produced before the
    evidence-fusion payload carried geometry (and a cross-check of the payload).
    """
    try:
        row = await pool.fetchrow(
            """
            SELECT ST_Y(ST_Centroid(geom)) AS centroid_lat,
                   ST_X(ST_Centroid(geom)) AS centroid_lon,
                   area_m2,
                   ST_AsGeoJSON(geom)      AS geom_geojson
            FROM spill_candidates
            WHERE candidate_id = $1::uuid
            """,
            candidate_id,
        )
    except Exception as exc:
        log.warning("candidate geometry lookup failed for %s: %s", candidate_id, exc)
        return None
    if row is None or row["centroid_lat"] is None:
        return None
    return {
        "centroid_lat": float(row["centroid_lat"]),
        "centroid_lon": float(row["centroid_lon"]),
        "area_km2": float(row["area_m2"]) / 1_000_000.0 if row["area_m2"] else 0.0,
        "geom_geojson": row["geom_geojson"],
    }


async def _get_or_create_spill_incident(
    pool, candidate_id: str, scene_id: str, confidence: float,
    acquisition_time: Optional[datetime], is_synthetic: bool,
    lat: float, lon: float, area_km2: float,
    geom_geojson: Optional[str] = None,
) -> Optional[tuple]:
    """
    Create a spill_incident row from the fused event + the REAL candidate
    polygon. When the candidate geometry (GeoJSON) is available the incident
    boundary is the detected slick polygon itself and area/centroid are derived
    from it geodesically; only when no geometry exists do we fall back to a
    1 km buffer circle around the centroid (clearly a lower bound).
    Returns the new spill_id (UUID str) or None on failure.
    """
    if geom_geojson:
        geom_sql = "ST_SetSRID(ST_GeomFromGeoJSON($8), 4326)"
    else:
        geom_sql = (
            "ST_Buffer(ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography, 1000)::geometry"
        )

    centroid_from_geom_sql = f"ST_Centroid({geom_sql})"
    # Keep the typed payload coordinates as a defensive fallback. Explicit
    # casts are required in the GeoJSON branch because asyncpg otherwise sees
    # $2/$3 as untyped parameters even though geometry supplies the centroid.
    lat_expr = f"COALESCE(ST_Y({centroid_from_geom_sql}), $2::double precision)"
    lon_expr = f"COALESCE(ST_X({centroid_from_geom_sql}), $3::double precision)"
    # Geodesic area of the actual polygon (m^2) — never trust a payload number.
    area_expr = f"COALESCE(ST_Area({geom_sql}::geography) / 1000000.0, $5::double precision)"

    try:
        # NOTE: asyncpg requires len(args) == highest referenced $n.
        # Branch A (GeoJSON): highest ref is $8 -> pass 8 args.
        # Branch B (buffer fallback): highest ref is $7 -> pass 7 args.
        spill_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"aqua-sentinel:{scene_id}:{candidate_id}"))
        args: list = [
            spill_id, lat, lon, acquisition_time, area_km2, confidence, scene_id
        ]
        if geom_geojson:
            args.append(geom_geojson)
        row = await pool.fetchrow(
            f"""
            INSERT INTO spill_incidents
                (id, detected_at, latitude, longitude, geom, centroid,
                 area_km2, confidence, source, source_image_id, status)
            VALUES (
                $1,
                COALESCE($4, NOW()),
                {lat_expr},
                {lon_expr},
                {geom_sql},
                {centroid_from_geom_sql},
                {area_expr},
                $6,
                'sar_satellite',
                $7,
                'detected'
            )
            ON CONFLICT (id) DO UPDATE SET status='detected'
            RETURNING id, latitude, longitude
            """,
            *args
        )
        if row:
            await pool.execute("UPDATE spill_incidents SET candidate_id=$2::uuid WHERE id=$1::uuid", str(row["id"]), candidate_id)
            log.info("Created/Found spill_incident id=%s", row['id'])
            return str(row["id"]), float(row["latitude"]), float(row["longitude"])
        return None
    except Exception as e:
        log.error("Failed to create spill_incident for candidate=%s: %s", candidate_id, e)
        return None


async def _fetch_candidate_vessels(
    pool, spill_lat: float, spill_lon: float,
    acquisition_time: datetime,
) -> List[Dict[str, Any]]:
    """
    Fetch all vessels that were within the spatial+temporal window.
    Returns list of dicts with vessel metadata and closest position.
    """
    window_h = ATTRIBUTION_TEMPORAL_WINDOW_H
    window_m = ATTRIBUTION_SPATIAL_WINDOW_M
    start_ts = acquisition_time - timedelta(hours=window_h)
    end_ts   = acquisition_time + timedelta(hours=window_h)

    rows = await pool.fetch(
        """
        SELECT
            v.id              AS vessel_id,
            v.mmsi,
            v.vessel_type,
            v.last_lat,
            v.last_lon,
            vp.latitude       AS pos_lat,
            vp.longitude      AS pos_lon,
            vp.timestamp      AS pos_ts,
            ST_Distance(
                vp.geom::geography,
                ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography
            ) AS distance_m
        FROM vessel_positions vp
        JOIN vessels v ON v.id = vp.vessel_id
        WHERE vp.timestamp BETWEEN $3 AND $4
          AND ST_DWithin(
                vp.geom::geography,
                ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography,
                $5
          )
        ORDER BY distance_m ASC
        """,
        spill_lat, spill_lon, start_ts, end_ts, window_m * 2,
    )
    return [dict(r) for r in rows]


async def _score_and_persist_vessel(
    pool, spill_id: str,
    spill_lat: float, spill_lon: float,
    vessel: Dict[str, Any],
    acquisition_time: datetime,
    weather: Dict[str, float],
    origin_prob: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Compute full 5-factor score for one vessel and upsert into attribution_results."""
    vessel_id  = vessel["vessel_id"]
    mmsi       = str(vessel["mmsi"])
    pos_lat    = float(vessel["pos_lat"])
    pos_lon    = float(vessel["pos_lon"])
    pos_ts     = vessel["pos_ts"]
    if pos_ts and pos_ts.tzinfo is None:
        pos_ts = pos_ts.replace(tzinfo=timezone.utc)

    # Factor 1: distance from position to spill at acquisition time
    dist_m = float(vessel["distance_m"]) if "distance_m" in vessel else 0.0
    d_score = score_distance(dist_m)

    # Factor 2: trajectory evidence was calculated by Evidence Fusion.
    closest_m = vessel.get("closest_approach_m")
    t_score = score_trajectory(float(closest_m) if closest_m is not None else None)

    # Factor 3: temporal coincidence was calculated by Evidence Fusion.
    time_gap_hours = vessel.get("time_gap_hours")
    ti_score = score_time(float(time_gap_hours) if time_gap_hours is not None else None)

    # Factor 4: behavioral anomaly evidence was calculated by Evidence Fusion.
    b_score = score_behavior(
        int(vessel.get("high_anomaly_count", 0)),
        int(vessel.get("medium_anomaly_count", 0)),
    )

    # Factor 5: backward propagation origin proximity
    if origin_prob and origin_prob.spatial_posterior:
        sp = origin_prob.spatial_posterior
        tp = origin_prob.temporal_posterior
        w_score = score_origin_proximity(
            vessel_lat=pos_lat, vessel_lon=pos_lon, vessel_time=pos_ts,
            origin_lat=sp.map_lat, origin_lon=sp.map_lon, origin_time=tp.map_release_time
        )
    else:
        # Fallback to simple wind drift
        elapsed_h = (acquisition_time - pos_ts).total_seconds() / 3600.0 if pos_ts else 0.0
        w_score = score_wind_drift(
            vessel_lat=pos_lat, vessel_lon=pos_lon,
            spill_lat=spill_lat, spill_lon=spill_lon,
            wind_speed_ms=weather.get("wind_speed_ms", 0.0),
            wind_dir_deg=weather.get("wind_dir_deg", 0.0),
            current_speed_ms=weather.get("current_speed_ms", 0.0),
            current_dir_deg=weather.get("current_dir_deg", 0.0),
            elapsed_hours=max(0.0, elapsed_h),
        )

    final = compute_attribution_score(d_score, t_score, ti_score, b_score, w_score)

    try:
        await pool.execute(
            """
            INSERT INTO attribution_results
                (spill_id, vessel_id, distance_score, trajectory_score,
                 wind_score, time_score, behavior_score, final_score, model_version)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (spill_id, vessel_id, model_version)
            DO UPDATE SET
                distance_score=EXCLUDED.distance_score,
                trajectory_score=EXCLUDED.trajectory_score,
                wind_score=EXCLUDED.wind_score,
                time_score=EXCLUDED.time_score,
                behavior_score=EXCLUDED.behavior_score,
                final_score=EXCLUDED.final_score,
                computed_at=NOW()
            """,
            spill_id, vessel_id, d_score, t_score, w_score, ti_score, b_score, final, MODEL_VERSION,
        )
    except Exception as e:
        log.error("attribution_results upsert failed vessel=%s spill=%s: %s", mmsi, spill_id, e)

    return {
        "vessel_id": vessel_id, "mmsi": mmsi,
        "distance_score": d_score, "trajectory_score": t_score,
        "time_score": ti_score, "behavior_score": b_score,
        "wind_score": w_score, "final_score": final,
        "label": attribution_label(final),
    }


def _normalise_candidate_vessel(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a vessel candidate from incident.fused.candidates into the
    shape expected by the existing attribution scoring pipeline."""
    if raw is None:
        return None
    vessel_id = raw.get("vessel_id")
    if vessel_id is None:
        raise ValueError("incident.fused candidate missing vessel_id")
    mmsi = raw.get("mmsi")
    if mmsi is None:
        raise ValueError(f"incident.fused candidate with vessel_id={vessel_id} missing mmsi")

    pos_lat = raw.get("position_lat")
    pos_lon = raw.get("position_lon")
    if pos_lat is None:
        pos_lat = raw.get("latitude")
    if pos_lon is None:
        pos_lon = raw.get("longitude")
    if pos_lat is None or pos_lon is None:
        raise ValueError(f"incident.fused candidate vessel_id={vessel_id} missing position_lat/position_lon")

    pos_ts = raw.get("position_timestamp")
    if pos_ts is not None and not isinstance(pos_ts, datetime):
        pos_ts = _parse_dt(pos_ts)

    distance_m = raw.get("distance_m")
    if distance_m is None:
        raise ValueError(f"incident.fused candidate vessel_id={vessel_id} missing distance_m")

    return {
        "vessel_id": int(vessel_id),
        "mmsi": str(mmsi),
        "vessel_type": raw.get("vessel_type"),
        "pos_lat": float(pos_lat),
        "pos_lon": float(pos_lon),
        "pos_ts": pos_ts,
        "distance_m": float(distance_m),
        "closest_approach_m": (
            float(raw["closest_approach_m"])
            if raw.get("closest_approach_m") is not None
            else None
        ),
        "time_gap_hours": (
            float(raw["time_gap_hours"])
            if raw.get("time_gap_hours") is not None
            else None
        ),
        "high_anomaly_count": int(raw.get("high_anomaly_count", 0)),
        "medium_anomaly_count": int(raw.get("medium_anomaly_count", 0)),
    }


async def _process_incident_fused(
    data: Dict[str, Any], pool, redis,
) -> None:
    candidate_id        = str(data.get("candidate_id") or "")
    scene_id            = str(data.get("scene_id") or "")
    confidence          = float(data.get("confidence") or 0.0)
    acquisition_time    = _parse_dt(data.get("acquisition_time"))
    is_synthetic        = str(data.get("is_synthetic", "false")).lower() in ("true", "1")

    if not candidate_id:
        log.warning("incident.fused message missing candidate_id")
        return

    acq = acquisition_time or datetime.now(timezone.utc)

    # Prefer geometry from the fused event; fall back to the DB row (covers
    # messages produced before evidence-fusion carried geometry, and guards
    # against payloads missing coordinates).
    payload_lat = data.get("lat")
    payload_lon = data.get("lon")
    geom_geojson = data.get("geom_geojson")

    if payload_lat is None or payload_lon is None or geom_geojson is None:
        db_geom = await _fetch_candidate_geometry(pool, candidate_id)
        if db_geom:
            payload_lat = payload_lat if payload_lat is not None else db_geom["centroid_lat"]
            payload_lon = payload_lon if payload_lon is not None else db_geom["centroid_lon"]
            area_val = float(data.get("area_km2") or 0.0)
            if area_val <= 0.0:
                payload_area = db_geom["area_km2"]
            else:
                payload_area = area_val
            geom_geojson = geom_geojson if geom_geojson is not None else db_geom["geom_geojson"]
        else:
            payload_area = float(data.get("area_km2") or 0.0)
    else:
        payload_area = float(data.get("area_km2") or 0.0)

    result = await _get_or_create_spill_incident(
        pool, candidate_id, scene_id, confidence, acq, is_synthetic,
        float(payload_lat or 0.0), float(payload_lon or 0.0), float(payload_area or 0.0),
        geom_geojson=geom_geojson,
    )
    if result is None:
        return

    spill_id, spill_lat, spill_lon = result
    STATE["last_candidate_id"] = candidate_id
    STATE["last_spill_id"] = spill_id

    candidates_raw = data.get("candidates")
    if isinstance(candidates_raw, str):
        candidates_raw = json.loads(candidates_raw)
    if candidates_raw is None:
        raise ValueError("incident.fused message missing candidates array; Evidence Fusion is required to provide candidate vessels")
    if not isinstance(candidates_raw, list):
        raise ValueError("incident.fused candidates must be a list")

    vessels = []
    for raw in candidates_raw:
        vessel = _normalise_candidate_vessel(raw)
        if vessel is not None:
            vessels.append(vessel)

    if not vessels:
        log.info("No candidate vessels provided for spill=%s — publishing with no attribution", spill_id)
        await publish_to_stream(redis, OUTPUT_STREAM, {
            "spill_id": spill_id,
            "candidate_id": candidate_id,
            "scene_id": scene_id,
            "spill_lat": spill_lat,
            "spill_lon": spill_lon,
            "acquisition_time": acq.isoformat(),
            "top_vessel_id": "",
            "top_vessel_mmsi": "",
            "top_score": 0.0,
            "top_label": "insufficient_evidence",
            "candidates_scored": 0,
            "is_synthetic": is_synthetic,
        })
        STATE["incidents_processed"] += 1
        return

    # Deduplicate by vessel_id — keep closest position per vessel
    seen = {}
    for v in vessels:
        vid = v["vessel_id"]
        if vid not in seen or v["distance_m"] < seen[vid]["distance_m"]:
            seen[vid] = v
    unique_vessels = list(seen.values())

    environment = data.get("environment") or {
        "wind_speed_ms": 0.0,
        "wind_dir_deg": 0.0,
        "current_speed_ms": 0.0,
        "current_dir_deg": 0.0,
    }
    if isinstance(environment, str):
        environment = json.loads(environment)

    origin_prob = await find_origin_probability(spill_lat, spill_lon, payload_area, acq)
    if origin_prob and origin_prob.spatial_posterior:
        sp = origin_prob.spatial_posterior
        tp = origin_prob.temporal_posterior
        origin_vessels = await _fetch_candidate_vessels(pool, sp.map_lat, sp.map_lon, tp.map_release_time)
        for v in origin_vessels:
            vid = v["vessel_id"]
            if vid not in seen:
                seen[vid] = v
        unique_vessels = list(seen.values())

    scored = []
    for vessel in unique_vessels[:20]:  # cap at 20 candidates
        result_v = await _score_and_persist_vessel(
            pool, spill_id, spill_lat, spill_lon, vessel, acq, environment, origin_prob
        )
        if result_v:
            scored.append(result_v)
            STATE["attributions_written"] += 1

    if not scored:
        top = {"vessel_id": "", "mmsi": "", "final_score": 0.0, "label": "insufficient_evidence"}
    else:
        top = max(scored, key=lambda x: x["final_score"])

    log.info(
        "Attribution: spill=%s top_vessel=%s score=%.3f label=%s (%d candidates)",
        spill_id, top.get("mmsi"), top["final_score"], top["label"], len(scored),
    )

    await publish_to_stream(redis, OUTPUT_STREAM, {
        "spill_id": spill_id,
        "candidate_id": candidate_id,
        "scene_id": scene_id,
        "spill_lat": spill_lat,
        "spill_lon": spill_lon,
        "acquisition_time": acq.isoformat(),
        "top_vessel_id": str(top.get("vessel_id") or ""),
        "top_vessel_mmsi": str(top.get("mmsi") or ""),
        "top_score": top["final_score"],
        "top_label": top["label"],
        "candidates_scored": len(scored),
        "is_synthetic": is_synthetic,
    })
    STATE["incidents_processed"] += 1


async def run_attribution_worker() -> None:
    pool = await create_pool()
    redis = await get_redis()
    await ensure_consumer_group(redis, INPUT_STREAM, CONSUMER_GROUP)
    log.info("%s worker started (spatial_window=%.0fm temporal_window=%.1fh).",
             SERVICE_NAME, ATTRIBUTION_SPATIAL_WINDOW_M, ATTRIBUTION_TEMPORAL_WINDOW_H)

    while True:
        STATE["heartbeat"] = time.time()
        messages = await consume_stream(
            redis, INPUT_STREAM, CONSUMER_GROUP, CONSUMER_NAME,
            count=10, block_ms=2000,
        )
        for msg in messages:
            try:
                await _process_incident_fused(msg["data"], pool, redis)
            except Exception as exc:
                STATE["failed"] += 1
                log.exception("Attribution failed for message %s: %s", msg.get("id"), exc)
