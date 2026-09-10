"""Lookalike-engine worker: Step 4 shape filtering + Step 5 texture/confidence.

Consumes ``spill.candidates.raw`` (one message per scene), retrieves the
scene-level raster artifact by ``scene_id``, then per candidate:

    Step 4: shape/lookalike heuristics -> status update
    Step 5: possible_slick candidates only -> raw-SAR GLCM texture -> heuristic
            confidence score -> confidence / classification_label /
            texture_features storage; possible_oil_spill results are published
            to ``spill.candidates.filtered``.

Never deletes candidates, never overwrites a later status, and never claims
confirmed oil.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from shared.artifacts import (
    candidate_pixel_mask,
    crop_region,
    geometry_to_pixel_bbox,
    load_scene_artifact,
)
from shared.db.connection import create_pool
from shared.redis_client import (
    consume_stream,
    ensure_consumer_group,
    get_redis,
    publish_to_stream,
)
from app.shape_filters import classify_candidate, compute_edge_descriptors, compute_shape_descriptors
from app.scoring import (
    CONFIDENCE_THRESHOLD,
    PASS_THROUGH_LABELS,
    score_candidate,
)
from app.texture import compute_glcm_features

log = logging.getLogger(__name__)

SERVICE_NAME = "lookalike-engine"
CONSUMER_GROUP = "lookalike-engine"
CONSUMER_NAME = "lookalike-worker"

CANDIDATES_RAW_STREAM = "spill.candidates.raw"
CANDIDATES_FILTERED_STREAM = "spill.candidates.filtered"

# Extra pixel padding around the candidate bbox so the ship-shadow heuristic can
# see a bright target up to ``adjacency_px`` away (default 5) plus margin.
PIXEL_PADDING = 7

GLCM_LEVELS = 32
TEXTURE_EVIDENCE_FIELDS = (
    "contrast", "homogeneity", "energy", "correlation",
    "mean_backscatter", "std_backscatter", "score_components", "shape",
    "edge", "context", "model_version",
)

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "messages_consumed": 0,
    "candidates_classified": 0,
    "candidates_scored": 0,
    "candidates_filtered_published": 0,
    "candidates_skipped": 0,
    "scenes_failed": 0,
    "last_scene_id": None,
    "last_processed_at": None,
}


def _artifact_root() -> str:
    return os.environ.get("SAR_ARTIFACT_ROOT", "/data/artifacts")


def _candidate_ids(data: Dict[str, Any]) -> List[str]:
    raw = data.get("candidate_ids")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        raise ValueError("spill.candidates.raw message is missing candidate_ids.")
    return [str(c) for c in raw]


_FETCH_CANDIDATE_SQL = """
    SELECT candidate_id, scene_id, acquisition_time, status, pixel_count, area_m2,
           classification_label, is_synthetic, ST_AsGeoJSON(geom) AS geojson,
           ST_X(ST_Centroid(geom)) AS centroid_lon,
           ST_Y(ST_Centroid(geom)) AS centroid_lat
    FROM spill_candidates
    WHERE candidate_id = $1
"""

_UPDATE_STATUS_SQL = """
    UPDATE spill_candidates
    SET status = $1::spill_candidate_status_enum
    WHERE candidate_id = $2 AND status = 'raw'
"""

_UPDATE_CLASSIFICATION_SQL = """
    UPDATE spill_candidates
    SET classification_label = $1::spill_candidate_status_enum,
        confidence = $2,
        texture_features = $3::jsonb
    WHERE candidate_id = $4
      AND status = 'possible_slick'
      AND classification_label IS NULL
"""

_UPDATE_PASSTHROUGH_SQL = """
    UPDATE spill_candidates
    SET classification_label = $1::spill_candidate_status_enum
    WHERE candidate_id = $2
      AND classification_label IS NULL
"""


async def _update_status(pool, candidate_id: str, label: str) -> bool:
    """Update a candidate's status unless it already left ``raw``."""
    row = await pool.execute(_UPDATE_STATUS_SQL, label, candidate_id)
    return "UPDATE 1" in row


async def _context_evidence(conn, row: Dict[str, Any]) -> Dict[str, Any]:
    """Measure nearby saved AIS evidence; absence means no contextual support."""
    nearest = await conn.fetchrow(
        """
        SELECT v.mmsi, v.name AS vessel_name, vp.timestamp,
               ST_Distance(vp.geom::geography, c.geom::geography) AS distance_m,
               ABS(EXTRACT(EPOCH FROM (vp.timestamp - c.acquisition_time))) / 3600.0 AS time_gap_hours
        FROM spill_candidates c
        JOIN vessel_positions vp
          ON vp.timestamp BETWEEN c.acquisition_time - INTERVAL '6 hours'
                              AND c.acquisition_time + INTERVAL '6 hours'
        JOIN vessels v ON v.id=vp.vessel_id
        WHERE c.candidate_id=$1::uuid
          AND ST_DWithin(vp.geom::geography, c.geom::geography, 20000.0)
        ORDER BY vp.geom <-> ST_Centroid(c.geom), time_gap_hours
        LIMIT 1
        """,
        str(row["candidate_id"]),
    )
    if not nearest:
        return {"score": 0.0, "source": "saved_ais", "nearby_vessel_found": False}
    distance_m = float(nearest["distance_m"])
    gap_h = float(nearest["time_gap_hours"])
    distance_support = max(0.0, 1.0 - distance_m / 20_000.0)
    time_support = max(0.0, 1.0 - gap_h / 6.0)
    return {
        "score": 0.6 * distance_support + 0.4 * time_support,
        "source": "saved_ais",
        "nearby_vessel_found": True,
        "nearby_vessel_mmsi": str(nearest["mmsi"]),
        "nearby_vessel_name": nearest["vessel_name"],
        "vessel_distance_m": distance_m,
        "time_gap_hours": gap_h,
        "position_timestamp": nearest["timestamp"].isoformat(),
    }


async def _resolve_candidate(
    candidate_row: Dict[str, Any],
    artifact_root: str,
) -> Dict[str, Any]:
    """Load the scene artifact and crop all pixel inputs for one candidate."""
    geometry = json.loads(candidate_row["geojson"])
    scene_id = candidate_row["scene_id"]

    artifact = load_scene_artifact(artifact_root, scene_id)
    if artifact is None:
        raise FileNotFoundError(
            f"scene artifact not found for scene_id={scene_id} (candidate "
            f"{candidate_row['candidate_id']})"
        )

    metadata = artifact["metadata"]
    bbox = geometry_to_pixel_bbox(
        geometry,
        metadata["affine"],
        padding=PIXEL_PADDING,
        shape=metadata["shape"],
    )

    raw_image = artifact.get("raw_image")
    return {
        "geometry": geometry,
        "dark_mask": crop_region(artifact["cleaned_mask"], bbox) & candidate_pixel_mask(geometry, metadata["affine"], bbox),
        "intensity": crop_region(artifact["filtered_image"], bbox),
        "bright_target": crop_region(artifact["bright_target_mask"], bbox),
        "raw_image": crop_region(raw_image, bbox) if raw_image is not None else None,
    }


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _filtered_event(data: Dict[str, Any], result) -> Dict[str, Any]:
    """One message per scored candidate published to spill.candidates.filtered."""
    event: Dict[str, Any] = {
        "candidate_id": str(result["candidate_id"]),
        "scene_id": result["scene_id"],
        "confidence": result["confidence"],
        "classification_label": result["classification_label"],
        "is_synthetic": _as_bool(data.get("is_synthetic"), default=False),
    }
    for key in ("geom_geojson", "centroid_lat", "centroid_lon", "area_m2", "pixel_count"):
        if result.get(key) is not None:
            # PostGIS NUMERIC comes back as Decimal. Emit JSON numbers, not
            # quoted Decimal strings that downstream float parsing rejects.
            event[key] = (int(result[key]) if key == "pixel_count" else
                          result[key] if key == "geom_geojson" else float(result[key]))
    texture = result.get("texture_features")
    if isinstance(texture, dict):
        event["texture_features"] = {
            key: texture[key]
            for key in TEXTURE_EVIDENCE_FIELDS
            if key in texture
        }
    for key in ("acquisition_time", "orbit", "polarization", "resolution"):
        if key in data:
            event[key] = data[key]
    return event


async def _process_candidates_message(data: Dict[str, Any], pool, redis) -> None:
    """Step 4 classify + Step 5 score candidates announced in one scene message."""
    scene_id = data.get("scene_id")
    try:
        candidate_ids = _candidate_ids(data)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed spill.candidates.raw message: {exc}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            for candidate_id in candidate_ids:
                row = await conn.fetchrow(_FETCH_CANDIDATE_SQL, candidate_id)
                if row is None:
                    log.warning("candidate %s not found; skipping", candidate_id)
                    STATE["candidates_skipped"] += 1
                    continue

                row = dict(row)
                candidate_id = str(row["candidate_id"])

                # Step 4: heuristic shape filtering (raw -> status).
                if row["status"] == "raw":
                    try:
                        resolved = await _resolve_candidate(row, _artifact_root())
                        label = classify_candidate(
                            {
                                "candidate_id": candidate_id,
                                "geometry": resolved["geometry"],
                                "pixel_count": int(row["pixel_count"]),
                            },
                            {
                                "dark_mask": resolved["dark_mask"],
                                # Segment on the denoised image, but measure
                                # radiometric contrast on the original pixels.
                                # Lee filtering mixes the boundary with nearby
                                # water and must not erase rejection evidence.
                                "intensity": resolved["raw_image"] if resolved["raw_image"] is not None else resolved["intensity"],
                            },
                            resolved["bright_target"],
                        )
                    except Exception as exc:
                        STATE["candidates_skipped"] += 1
                        log.exception(
                            "Step 4 classification failed for candidate %s scene=%s: %s",
                            candidate_id, scene_id, exc,
                        )
                        continue
                    await _update_status(conn, candidate_id, label)
                    row["status"] = label
                    STATE["candidates_classified"] += 1

                if row.get("classification_label") is not None:
                    STATE["candidates_skipped"] += 1  # already scored / pass-through
                    continue

                # Step 5: only possible_slick candidates are scored.
                if row["status"] in PASS_THROUGH_LABELS:
                    await conn.execute(_UPDATE_PASSTHROUGH_SQL, row["status"], candidate_id)
                    STATE["candidates_skipped"] += 1
                    continue

                if row["status"] == "possible_slick":
                    try:
                        resolved = await _resolve_candidate(row, _artifact_root())
                        if resolved["raw_image"] is None:
                            raise FileNotFoundError(
                                "raw SAR artifact (raw_image.npy) missing; Step 5 scoring "
                                "requires pre-despeckle values, not the Lee-filtered image."
                            )
                        candidate = {
                            "candidate_id": candidate_id,
                            "geometry": resolved["geometry"],
                            "pixel_count": int(row["pixel_count"]),
                            "area_m2": float(row["area_m2"]) if row["area_m2"] is not None else None,
                            "classification_label": row["status"],
                        }
                        shape = compute_shape_descriptors(candidate, resolved["dark_mask"])
                        edge = compute_edge_descriptors(resolved["raw_image"], resolved["dark_mask"])
                        context = await _context_evidence(conn, row)
                        candidate["shape_features"] = shape
                        texture = compute_glcm_features(resolved["raw_image"], resolved["dark_mask"], levels=GLCM_LEVELS)
                        scored = score_candidate(candidate, texture, context_score=context["score"])
                        texture["score_components"] = scored["score_components"]
                        texture["shape"] = shape
                        texture["edge"] = edge
                        texture["context"] = context
                        texture["model_version"] = "sar-lookalike-heuristic-v1"
                    except Exception as exc:
                        STATE["candidates_skipped"] += 1
                        log.exception(
                            "Step 5 scoring failed for candidate %s scene=%s: %s",
                            candidate_id, scene_id, exc,
                        )
                        continue

                    updated = await conn.execute(
                        _UPDATE_CLASSIFICATION_SQL,
                        scored["classification_label"],
                        scored["confidence"],
                        json.dumps(texture),
                        candidate_id,
                    )
                    if "UPDATE 1" in updated:
                        STATE["candidates_scored"] += 1
                        log.info(
                            "candidate %s scene=%s confidence=%.3f label=%s",
                            candidate_id, scene_id, scored["confidence"],
                            scored["classification_label"],
                        )
                        if scored["classification_label"] == "possible_oil_spill":
                            await publish_to_stream(
                                redis,
                                CANDIDATES_FILTERED_STREAM,
                                _filtered_event(data, {
                                    "candidate_id": candidate_id,
                                    "scene_id": row["scene_id"],
                                    "confidence": scored["confidence"],
                                    "classification_label": scored["classification_label"],
                                    "geom_geojson": row["geojson"],
                                    "centroid_lat": row["centroid_lat"],
                                    "centroid_lon": row["centroid_lon"],
                                    "area_m2": row["area_m2"],
                                    "pixel_count": row["pixel_count"],
                                    "texture_features": texture,
                                }),
                            )
                            STATE["candidates_filtered_published"] += 1
                    else:
                        STATE["candidates_skipped"] += 1
                    continue

                STATE["candidates_skipped"] += 1


async def run_lookalike_worker() -> None:
    """Consume spill.candidates.raw with the shared consumer pattern."""
    pool = await create_pool()
    redis = await get_redis()
    await ensure_consumer_group(redis, CANDIDATES_RAW_STREAM, CONSUMER_GROUP)
    log.info("%s worker started.", SERVICE_NAME)
    log.info(
        "Step 5 filtered stream: %s (CONFIDENCE_THRESHOLD=%s)",
        CANDIDATES_FILTERED_STREAM,
        CONFIDENCE_THRESHOLD,
    )

    while True:
        STATE["heartbeat"] = time.time()
        messages = await consume_stream(
            redis,
            CANDIDATES_RAW_STREAM,
            CONSUMER_GROUP,
            CONSUMER_NAME,
            count=20,
            block_ms=2000,
        )
        for message in messages:
            scene_id = message["data"].get("scene_id")
            try:
                await _process_candidates_message(message["data"], pool, redis)
            except Exception as exc:
                STATE["scenes_failed"] += 1
                log.exception(
                    "lookalike processing failed scene=%s: %s", scene_id, exc
                )
            finally:
                STATE["messages_consumed"] += 1
                STATE["last_scene_id"] = scene_id
                STATE["last_processed_at"] = datetime.now(timezone.utc).isoformat()
