"""Evidence-fusion worker: fuse SAR candidates with nearby vessel-risk context.

Consumes:

- ``spill.candidates.filtered`` (Step 5 output) — the SAR evidence to fuse.
- ``vessel.risk`` (signal kept for stream continuity) — vessel-risk evidence is
  resolved from PostGIS at correlation time, so vessel.risk messages are drained
  and counted without secondary processing.

For each filtered SAR candidate the worker:

1. looks up the candidate centroid + acquisition time in PostGIS
2. queries high-risk vessels (tier HIGH/CRITICAL) within the spatial/temporal
   correlation windows using PostGIS geography ST_DWithin
3. selects the deterministically nearest correlated vessel (nullable)
4. publishes the fused evidence to ``incident.fused``

This is fusion only — ``correlated_vessel_id`` is contextual evidence, never
source attribution.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from shared.db.connection import create_pool
from shared.redis_client import (
    consume_stream,
    ensure_consumer_group,
    get_redis,
    publish_to_stream,
)
from app.evidence import (
    build_vessel_correlation_sql,
    candidate_lookup_sql,
    correlation_windows,
    fuse_evidence,
    select_correlated_vessels,
)

log = logging.getLogger(__name__)

SERVICE_NAME = "evidence-fusion"
CONSUMER_GROUP = "evidence-fusion"

VESSEL_RISK_STREAM = "vessel.risk"
FILTERED_STREAM = "spill.candidates.filtered"
FUSED_STREAM = "incident.fused"
# Published by the `eo` service once its (async, possibly slow, possibly
# never-arriving) Sentinel-2 check resolves for a candidate. Fusion itself
# never waits on this — see fuse_evidence()'s docstring — this is late
# enrichment only, for consumers that want to react when it does arrive.
OPTICAL_STREAM = "optical.confirmed"
ENRICHED_STREAM = "incident.enriched"

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "vessel_risk_messages": 0,
    "filtered_messages": 0,
    "candidates_fused": 0,
    "candidates_with_vessel": 0,
    "scenes_failed": 0,
    "last_candidate_id": None,
    "last_processed_at": None,
}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _candidate_from_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise the Step 5 payload into the fuse_evidence candidate dict."""
    candidate = {
        "candidate_id": str(data["candidate_id"]),
        "scene_id": str(data["scene_id"]),
        "confidence": float(data["confidence"]),
        "classification_label": str(data["classification_label"]),
        "acquisition_time": data.get("acquisition_time"),
        "is_synthetic": _as_bool(data.get("is_synthetic"), default=False),
    }
    for key in ("orbit", "polarization", "resolution"):
        if key in data:
            candidate[key] = data[key]
    for key in (
        "geom_geojson", "centroid_lat", "centroid_lon", "area_m2", "pixel_count",
        "texture_features",
    ):
        if key in data:
            candidate[key] = data[key]
    return candidate


async def _fetch_anomaly_counts(
    pool, mmsi: str, acquisition_time: datetime,
) -> tuple[int, int]:
    """Count HIGH and MEDIUM anomaly events in the six hours before acquisition."""
    since = acquisition_time - timedelta(hours=6)
    rows = await pool.fetch(
        "SELECT severity FROM anomaly_events WHERE mmsi=$1 AND window_start BETWEEN $2 AND $3",
        mmsi, since, acquisition_time,
    )
    high = sum(1 for row in rows if row.get("severity") == "HIGH")
    medium = sum(1 for row in rows if row.get("severity") == "MEDIUM")
    return high, medium


async def _fetch_environment(
    pool, spill_lat: float, spill_lon: float, acquisition_time: datetime,
) -> Dict[str, Any]:
    """Fetch the nearest environmental sample using the existing lookup semantics."""
    row = await pool.fetchrow(
        """
        SELECT wind_speed_kmh, wind_direction_deg, current_speed_ms,
               current_direction_deg, timestamp, source
        FROM environmental_conditions
        WHERE timestamp BETWEEN $3::timestamptz - INTERVAL '3 hours'
                            AND $3::timestamptz + INTERVAL '3 hours'
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint($2, $1), 4326)
        LIMIT 1
        """,
        spill_lat, spill_lon, acquisition_time,
    )
    if row:
        return {
            "wind_speed_ms": float(row["wind_speed_kmh"] or 0) / 3.6,
            "wind_dir_deg": float(row["wind_direction_deg"] or 0),
            "current_speed_ms": float(row["current_speed_ms"] or 0),
            "current_dir_deg": float(row["current_direction_deg"] or 0),
            "timestamp": row["timestamp"].isoformat() if row["timestamp"] is not None else None,
            "source": row["source"],
        }
    return {
        "wind_speed_ms": 0.0,
        "wind_dir_deg": 0.0,
        "current_speed_ms": 0.0,
        "current_dir_deg": 0.0,
        "timestamp": None,
        "source": None,
    }


def _optical_from_row(row) -> Dict[str, Any]:
    """Best-effort optical evidence from the spill_candidates row.

    Usually still empty at fusion time (see fuse_evidence's docstring) — the
    eo service runs asynchronously and may take minutes (or find no
    cloud-free scene at all). That is expected, not an error.
    """
    checked_at = row["optical_checked_at"] if row is not None else None
    if checked_at is None:
        return {"checked": False, "cloud_free": None, "oil_probability": None, "predicted_class": None}
    return {
        "checked": True,
        "cloud_free": row["optical_cloud_free"],
        "oil_probability": float(row["optical_oil_probability"]) if row["optical_oil_probability"] is not None else None,
        "predicted_class": row["optical_predicted_class"],
        "checked_at": checked_at.isoformat(),
    }


async def _handle_filtered_candidate(data: Dict[str, Any], pool, redis) -> None:
    """Fuse one spill.candidates.filtered message and publish incident.fused."""
    candidate = _candidate_from_payload(data)
    candidate_id = candidate["candidate_id"]

    spatial_m, temporal_hours = correlation_windows()

    row = await pool.fetchrow(candidate_lookup_sql(), candidate_id)
    if row is None:
        # Candidate row not found (e.g. deleted or never persisted). Not a fuse
        # error for other candidates, but no geometry means no correlation.
        log.warning("candidate %s not found in spill_candidates; skipping fusion", candidate_id)
        STATE["candidates_fused"] += 1
        fused = fuse_evidence(candidate, None, optical=_optical_from_row(None))
        await publish_to_stream(redis, FUSED_STREAM, fused)
        STATE["last_candidate_id"] = candidate_id
        STATE["last_processed_at"] = datetime.now(timezone.utc).isoformat()
        return

    acq = row["acquisition_time"]
    if acq.tzinfo is None:
        acq = acq.replace(tzinfo=timezone.utc)
    start_ts = acq - timedelta(hours=temporal_hours)
    end_ts = acq + timedelta(hours=temporal_hours)

    centroid_lat = float(row["centroid_lat"]) if row["centroid_lat"] is not None else None
    centroid_lon = float(row["centroid_lon"]) if row["centroid_lon"] is not None else None
    area_m2 = float(row["area_m2"]) if row["area_m2"] is not None else None
    geom_geojson = row["geom_geojson"]
    if centroid_lat is not None and centroid_lon is not None:
        environment = await _fetch_environment(pool, centroid_lat, centroid_lon, acq)
    else:
        environment = {
            "wind_speed_ms": 0.0,
            "wind_dir_deg": 0.0,
            "current_speed_ms": 0.0,
            "current_dir_deg": 0.0,
            "timestamp": None,
            "source": None,
        }

    records = await pool.fetch(
        build_vessel_correlation_sql(),
        start_ts,
        end_ts,
        centroid_lon,
        centroid_lat,
        float(spatial_m),
    )
    vessel_records = [dict(r) for r in records]

    candidates = select_correlated_vessels(
        vessel_records,
        spatial_window_m=spatial_m,
        temporal_window_hours=temporal_hours,
        candidate_acquisition=acq,
    )

    for vessel in candidates:
        high_count, medium_count = await _fetch_anomaly_counts(
            pool, str(vessel["mmsi"]), acq,
        )
        vessel["high_anomaly_count"] = high_count
        vessel["medium_anomaly_count"] = medium_count

    fused = fuse_evidence(
        candidate,
        candidates,
        centroid_lat=float(candidate.get("centroid_lat", centroid_lat)) if candidate.get("centroid_lat", centroid_lat) is not None else None,
        centroid_lon=float(candidate.get("centroid_lon", centroid_lon)) if candidate.get("centroid_lon", centroid_lon) is not None else None,
        area_m2=float(candidate.get("area_m2", area_m2)) if candidate.get("area_m2", area_m2) is not None else None,
        geom_geojson=candidate.get("geom_geojson", geom_geojson),
        environment=environment,
        optical=_optical_from_row(row),
    )
    await publish_to_stream(redis, FUSED_STREAM, fused)

    STATE["candidates_fused"] += 1
    STATE["candidates_with_vessel"] += len(candidates)
    STATE["last_candidate_id"] = candidate_id
    STATE["last_processed_at"] = datetime.now(timezone.utc).isoformat()
    log.info(
        "fused candidate=%s candidate_count=%s",
        candidate_id,
        len(candidates),
    )


async def _process_optical_message(data: Dict[str, Any], pool=None, redis=None) -> None:
    """Late enrichment: the eo service resolved (or gave up on) a candidate.

    spill_candidates itself was already updated directly by the eo service
    (it owns those columns); this just tells anyone downstream who already
    reacted to the original incident.fused that optical evidence is now
    available, without re-running vessel correlation or re-publishing a full
    incident.fused record.
    """
    STATE["optical_messages"] = STATE.get("optical_messages", 0) + 1
    candidate_id = data.get("candidate_id")
    if candidate_id is None or redis is None:
        return
    await publish_to_stream(redis, ENRICHED_STREAM, {
        "candidate_id": str(candidate_id),
        "optical": {
            "checked": True,
            "cloud_free": _as_bool(data.get("cloud_free"), default=False) if "cloud_free" in data else None,
            "oil_probability": float(data["oil_probability"]) if data.get("oil_probability") is not None else None,
            "predicted_class": data.get("predicted_class"),
        },
    })


async def _process_filtered_message(data: Dict[str, Any], pool, redis) -> None:
    STATE["filtered_messages"] += 1
    await _handle_filtered_candidate(data, pool, redis)


async def _process_vessel_risk_message(data: Dict[str, Any], pool=None, redis=None) -> None:
    """Drain vessel.risk messages.

    Vessel-risk evidence is resolved from PostGIS at fusion time (see
    ``build_vessel_correlation_sql``), so a vessel.risk message is acknowledged
    and counted here without a secondary processing path.
    """
    STATE["vessel_risk_messages"] += 1
    mmsi = data.get("mmsi")
    log.debug("vessel.risk message for mmsi=%s (counted; correlation is DB-driven)", mmsi)


async def run_evidence_worker() -> None:
    """Consume vessel.risk and spill.candidates.filtered (shared pattern)."""
    pool = await create_pool()
    redis = await get_redis()
    for stream in (VESSEL_RISK_STREAM, FILTERED_STREAM, OPTICAL_STREAM):
        await ensure_consumer_group(redis, stream, CONSUMER_GROUP)
    spatial_m, temporal_hours = correlation_windows()
    log.info(
        "%s worker started (spatial_window_m=%.0f, temporal_window_hours=%.1f)",
        SERVICE_NAME,
        spatial_m,
        temporal_hours,
    )

    while True:
        STATE["heartbeat"] = time.time()
        for stream, handler, consumer in (
            (VESSEL_RISK_STREAM, _process_vessel_risk_message, "evidence-vessel-risk"),
            (FILTERED_STREAM, _process_filtered_message, "evidence-filtered"),
            (OPTICAL_STREAM, _process_optical_message, "evidence-optical"),
        ):
            messages = await consume_stream(
                redis,
                stream,
                CONSUMER_GROUP,
                consumer,
                count=20,
                block_ms=300,
            )
            for message in messages:
                candidate_id = message["data"].get("candidate_id")
                try:
                    await handler(message["data"], pool, redis)
                except Exception as exc:
                    STATE["scenes_failed"] += 1
                    log.exception(
                        "evidence-fusion failed stream=%s candidate=%s: %s",
                        stream,
                        candidate_id,
                        exc,
                    )