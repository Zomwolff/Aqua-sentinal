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
    select_correlated_vessel,
)

log = logging.getLogger(__name__)

SERVICE_NAME = "evidence-fusion"
CONSUMER_GROUP = "evidence-fusion"

VESSEL_RISK_STREAM = "vessel.risk"
FILTERED_STREAM = "spill.candidates.filtered"
FUSED_STREAM = "incident.fused"

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
    return candidate


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
        fused = fuse_evidence(candidate, None)
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

    records = await pool.fetch(
        build_vessel_correlation_sql(),
        start_ts,
        end_ts,
        centroid_lon,
        centroid_lat,
        float(spatial_m),
    )
    vessel_records = [dict(r) for r in records]

    correlated = select_correlated_vessel(
        vessel_records,
        spatial_window_m=spatial_m,
        temporal_window_hours=temporal_hours,
        candidate_acquisition=acq,
    )

    fused = fuse_evidence(
        candidate,
        correlated,
        centroid_lat=centroid_lat,
        centroid_lon=centroid_lon,
        area_m2=area_m2,
        geom_geojson=geom_geojson,
    )
    await publish_to_stream(redis, FUSED_STREAM, fused)

    STATE["candidates_fused"] += 1
    if correlated is not None:
        STATE["candidates_with_vessel"] += 1
    STATE["last_candidate_id"] = candidate_id
    STATE["last_processed_at"] = datetime.now(timezone.utc).isoformat()
    log.info(
        "fused candidate=%s correlated_vessel_id=%s",
        candidate_id,
        fused["correlated_vessel_id"],
    )


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
    for stream in (VESSEL_RISK_STREAM, FILTERED_STREAM):
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