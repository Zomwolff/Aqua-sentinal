"""SAR worker: GeoTIFF -> Lee filter -> dark segmentation + CFAR -> morphology -> polygonize."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List

import numpy as np
import rasterio
from rasterio.warp import transform_geom
from shapely.geometry import mapping, shape
from shapely.geometry.polygon import orient

from shared.artifacts import save_scene_artifact
from shared.db.connection import create_pool
from shared.redis_client import (
    consume_stream,
    ensure_consumer_group,
    get_redis,
    publish_to_stream,
)
from shared.spatial.constants import SENTINEL1_PIXEL_SIZE_M
from shared.spatial.geo import area_m2_from_geometry
from app.cfar import cfar_detect
from app.despeckle import lee_filter
from app.morphology import clean_mask
from app.polygonize import extract_candidates
from app.segmentation import dark_region_mask


log = logging.getLogger(__name__)

SERVICE_NAME = "sar-spill-intelligence"
CONSUMER_GROUP = "sar-spill-intelligence"
CONSUMER_NAME = "sar-spill-worker"

CANDIDATES_RAW_STREAM = "spill.candidates.raw"

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "messages_consumed": 0,
    "scenes_processed": 0,
    "scenes_failed": 0,
    "last_scene_id": None,
    "last_processed_at": None,
    "last_raster_metadata": None,
    "last_artifact_path": None,
}


@dataclass
class ProcessedSarScene:
    """In-memory handoff point for STEP 3 morphology and polygonization."""

    scene_metadata: Dict[str, Any]
    raster_metadata: Dict[str, Any]
    raw_image: np.ndarray
    filtered_image: np.ndarray
    binary_mask: np.ndarray


def _parse_scene_metadata(raw_metadata: Any) -> Dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if not isinstance(raw_metadata, str) or not raw_metadata:
        raise ValueError("sar.clean message is missing scene_metadata.")
    decoded = json.loads(raw_metadata)
    if not isinstance(decoded, dict):
        raise ValueError("sar.clean scene_metadata must decode to an object.")
    return decoded


# _load_and_detect logic moved into _process_sar_message for step-by-step event emission)


def _acquisition_datetime(scene_metadata: Dict[str, Any]) -> datetime:
    raw = scene_metadata.get("acquisition_time")
    if not raw:
        raise ValueError("scene_metadata is missing acquisition_time.")
    if isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def _is_synthetic(scene_metadata: Dict[str, Any]) -> bool:
    """Return the candidate provenance flag from the ingestion message.

    The source of truth is the ``is_synthetic`` field in the ingestion
    metadata; it is NEVER inferred from scene_id or filenames.
    """
    value = scene_metadata.get("is_synthetic", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _candidates_raw_event(
    scene_metadata: Dict[str, Any],
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """One message per scene: candidate IDs plus existing scene metadata."""
    event: Dict[str, Any] = {
        "scene_id": scene_metadata["scene_id"],
        "acquisition_time": scene_metadata.get("acquisition_time"),
        "candidate_ids": [str(c["candidate_id"]) for c in candidates],
        "is_synthetic": _is_synthetic(scene_metadata),
    }
    for key in ("orbit", "polarization", "resolution"):
        if key in scene_metadata:
            event[key] = scene_metadata[key]
    return event


_INSERT_CANDIDATE_SQL = """
    INSERT INTO spill_candidates
        (candidate_id, scene_id, acquisition_time, geom, area_m2, pixel_count,
         status, created_at, is_synthetic)
    VALUES ($1, $2, $3,
            ST_SetSRID(ST_GeomFromGeoJSON($4), 4326),
            $5, $6, $7::spill_candidate_status_enum, NOW(), $8)
"""


async def _persist_candidates(
    pool,
    scene_id: str,
    acquisition_time: datetime,
    candidates: List[Dict[str, Any]],
    is_synthetic: bool = False,
) -> None:
    """Write all candidates for one scene in a single transaction.

    Errors propagate to the caller so a failed DB write is never hidden.
    """
    if not candidates:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            for candidate in candidates:
                await conn.execute(
                    _INSERT_CANDIDATE_SQL,
                    candidate["candidate_id"],
                    scene_id,
                    acquisition_time,
                    json.dumps(candidate["geometry"]),
                    candidate["area_m2"],
                    candidate["pixel_count"],
                    "raw",
                    is_synthetic,
                )


def _min_area_m2_config() -> float:
    """Return the explicit minimum spill area (m²) from configuration.

    Read from the ``SAR_MIN_AREA_M2`` environment variable at the worker/config
    layer. There is no hardcoded default: the threshold must come from
    validation data. Raises ``ValueError`` (a configuration error) when the
    variable is missing, empty, or not a positive finite number, which fails the
    Step 3 processing path rather than hiding it.
    """
    raw = os.environ.get("SAR_MIN_AREA_M2")
    if not raw or raw.strip() == "":
        raise ValueError(
            "SAR_MIN_AREA_M2 is not configured. Set it from validation data "
            "before spill-candidate persistence; no default noise-area "
            "threshold may be invented."
        )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"SAR_MIN_AREA_M2 is invalid: {raw!r}. It must be configured as a "
            "positive number of square metres (float)."
        )
    if not np.isfinite(value) or value <= 0:
        raise ValueError(
            f"SAR_MIN_AREA_M2 must be a positive finite number, got {value!r}."
        )
    return value


def _bright_target_threshold_config() -> float:
    """Return the explicit high-backscatter threshold for bright-target masks.

    Read from ``SAR_BRIGHT_TARGET_THRESHOLD`` at the worker/config layer. There
    is no hardcoded scientific value: the threshold must come from
    validation/configuration. Missing/invalid configuration raises a clear
    error that fails the Step 3 path.
    """
    raw = os.environ.get("SAR_BRIGHT_TARGET_THRESHOLD")
    if not raw or raw.strip() == "":
        raise ValueError(
            "SAR_BRIGHT_TARGET_THRESHOLD is not configured. Set it from "
            "validation data before generating scene artifacts; no bright-target "
            "threshold may be invented."
        )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"SAR_BRIGHT_TARGET_THRESHOLD is invalid: {raw!r}. It must be "
            "configured as a positive finite number (float)."
        )
    if not np.isfinite(value) or value <= 0:
        raise ValueError(
            f"SAR_BRIGHT_TARGET_THRESHOLD must be a positive finite number, got {value!r}."
        )
    return value


def _artifact_root() -> str:
    return os.environ.get("SAR_ARTIFACT_ROOT", "/data/artifacts")


# Candidate physical area is computed with the shared geography-cast SQL helper.
# ST_GeomFromGeoJSON($1) carries SRID 4326; ST_SetSRID(..., 4326) + ::geography
# then yields the geodesic area in m². Never degree² (see docs/spatial.md).
_AREA_M2_SQL = "SELECT " + area_m2_from_geometry("ST_GeomFromGeoJSON($1)")


def _reproject_candidates_to_wgs84(candidates, source_crs: str) -> None:
    """Convert raster-space candidate geometry to GeoJSON lon/lat in place.

    ``extract_candidates`` applies the raster affine transform, so its output
    remains in the raster's native CRS. Sentinel-1 downloads commonly use a
    UTM CRS (for Mumbai, EPSG:32643); treating those metre coordinates as
    EPSG:4326 makes the subsequent PostGIS geography cast invalid.
    """
    if not source_crs:
        raise ValueError("SAR raster has no CRS; candidates cannot be reprojected.")

    for candidate in candidates:
        geometry = transform_geom(
            source_crs,
            "EPSG:4326",
            candidate["geometry"],
            precision=8,
        )
        polygon = shape(geometry)
        if polygon.geom_type != "Polygon" or polygon.is_empty:
            raise ValueError("SAR candidate did not reproject to a valid Polygon.")

        # GeoJSON's right-hand rule: exterior counter-clockwise, holes
        # clockwise. This also prevents PostGIS geography from interpreting a
        # small candidate as the complement of the globe.
        candidate["geometry"] = mapping(orient(polygon, sign=1.0))
        candidate["centroid"] = transform_geom(
            source_crs,
            "EPSG:4326",
            candidate["centroid"],
            precision=8,
        )


async def _resolve_candidate_areas(pool, candidates) -> None:
    """Stamp each candidate's ``area_m2`` via PostGIS geography.

    Uses the existing ``area_m2_from_geometry`` geography-cast path for every
    m² value. Errors propagate so a failed area computation is never hidden.
    Polygon extraction stays DB-free (see ``extract_candidates``).
    """
    for candidate in candidates:
        area_m2 = await pool.fetchval(
            _AREA_M2_SQL,
            json.dumps(candidate["geometry"]),
        )
        candidate["area_m2"] = float(area_m2)


async def _process_sar_message(
    data: Dict[str, Any],
    pool,
    redis,
) -> None:
    """Process one sar.clean message through STEP 3 (morphology + polygonize)."""
    raster_path = data.get("raster_path")
    scene_id = None
    try:
        if not isinstance(raster_path, str) or not raster_path:
            raise ValueError("sar.clean message is missing raster_path.")

        scene_metadata = _parse_scene_metadata(data.get("scene_metadata"))
        scene_id = scene_metadata.get("scene_id")

        # Step 3 gates: the physical minimum spill area and the bright-target
        # backscatter threshold must be explicitly configured from validation
        # data. Missing/invalid configuration raises and fails the Step 3 path
        # below (scenes_failed), never silently processing without persistence
        # or artifact generation.
        min_area_m2 = _min_area_m2_config()
        bright_threshold = _bright_target_threshold_config()

        await redis.publish("sar.tasking.events", json.dumps({"scene_id": scene_id, "step": "sar_tasking"}))
        await asyncio.sleep(0.5)

        # STEP 1: Fetching / Reading
        await redis.publish("sar.tasking.events", json.dumps({"scene_id": scene_id, "step": "sar_fetching"}))
        
        def _read_raster():
            with rasterio.open(raster_path) as dataset:
                if dataset.count < 1:
                    raise ValueError("GeoTIFF has no raster bands.")
                masked = dataset.read(1, masked=True)
                values = np.asarray(masked.filled(np.nan), dtype=np.float64)
                finite = np.isfinite(values)
                if not finite.any():
                    raise ValueError("GeoTIFF contains no finite VV pixels.")
                fill_value = float(np.median(values[finite]))
                working_image = np.where(finite, values, fill_value)
                raster_metadata = {
                    "crs": dataset.crs.to_string() if dataset.crs else None,
                    "transform": tuple(dataset.transform),
                    "width": dataset.width,
                    "height": dataset.height,
                    "bounds": tuple(dataset.bounds),
                    "resolution": tuple(dataset.res),
                }
                return working_image, finite, raster_metadata
        
        working_image, finite, raster_metadata = await asyncio.to_thread(_read_raster)
        raw_image = working_image
        await asyncio.sleep(0.5)

        # STEP 2: Despeckling — intensity (linear-power) domain Lee filter;
        # speckle is multiplicative in power, so the Lee MMSE model is applied
        # there and the result converted back to dB.
        await redis.publish("sar.tasking.events", json.dumps({"scene_id": scene_id, "step": "sar_despeckling"}))
        filtered_image = await asyncio.to_thread(lee_filter, working_image, 5, "linear")
        await asyncio.sleep(0.5)

        # STEP 3: CFAR / Dark Region
        # CFAR threshold multiplier k is env-tunable (SAR_CFAR_K, default 2.5)
        # so it can be calibrated against real Mumbai Sentinel-1 scenes without
        # a code change.
        cfar_k = float(os.environ.get("SAR_CFAR_K", 2.5))
        await redis.publish("sar.tasking.events", json.dumps({"scene_id": scene_id, "step": "sar_cfar"}))
        dark_mask = await asyncio.to_thread(dark_region_mask, filtered_image)
        anomaly_mask = await asyncio.to_thread(cfar_detect, filtered_image, 3, 15, cfar_k)
        binary_mask = (dark_mask | anomaly_mask) & finite
        await asyncio.sleep(0.5)

        # STEP 3 Step A — morphological cleaning
        await redis.publish("sar.tasking.events", json.dumps({"scene_id": scene_id, "step": "sar_morphology"}))
        cleaned_mask = await asyncio.to_thread(
            clean_mask,
            binary_mask,
            open_size=3,
            close_size=5,
        )
        await asyncio.sleep(0.5)

        # STEP 3 Step B — connected-component polygonization
        await redis.publish("sar.tasking.events", json.dumps({"scene_id": scene_id, "step": "sar_polygonize"}))
        candidates = await asyncio.to_thread(
            extract_candidates,
            mask=cleaned_mask,
            transform=raster_metadata["transform"],
            min_area_m2=min_area_m2,
            pixel_size_m=SENTINEL1_PIXEL_SIZE_M,
        )
        _reproject_candidates_to_wgs84(candidates, raster_metadata["crs"])

        # Scene-level artifact (step 4 dependency): filtered intensity, cleaned
        # mask, and bright-target mask, retrievable by scene_id. Raster pixels
        # are never written to PostgreSQL; one artifact bundle per processed
        # scene on the shared artifact volume. Fail fast so we never persist
        # candidates or publish candidate IDs without a retrievable artifact.
        affine = tuple(raster_metadata["transform"])
        if len(affine) == 9:
            affine = affine[:6]
        bright_target_mask = filtered_image > bright_threshold
        artifact_path = await asyncio.to_thread(
            save_scene_artifact,
            _artifact_root(),
            scene_id,
            raw_image=raw_image,
            filtered_image=filtered_image,
            cleaned_mask=cleaned_mask,
            bright_target_mask=bright_target_mask,
            affine=list(affine),
            shape=binary_mask.shape,
            crs=raster_metadata.get("crs"),
        )

        # STEP 4 support — vectorize bright targets (possible vessels) and
        # publish them to sar.objects so the dark-vessel detector's SAR
        # correlation has real coordinates to join against vessel_positions.
        try:
            from app.bright_targets import extract_bright_targets

            bright_targets = await asyncio.to_thread(
                extract_bright_targets,
                bright_target_mask,
                list(affine),
                SENTINEL1_PIXEL_SIZE_M,
            )
            if bright_targets:
                await redis.xadd(
                    "sar.objects",
                    {"data": json.dumps({
                        "scene_id": scene_id,
                        "acquisition_time": scene_metadata.get("acquisition_time"),
                        "is_synthetic": scene_metadata.get("is_synthetic", False),
                        "objects": bright_targets[:200],
                    })},
                )
                STATE["bright_targets_published"] = (
                    STATE.get("bright_targets_published", 0) + len(bright_targets)
                )
        except Exception as exc:
            log.warning("sar.objects publish failed for %s: %s", scene_id, exc)

        # STEP 3 Step C — resolve physical area via the shared PostGIS
        # geography path, then persist all candidate rows for this scene in a
        # single transaction before emitting any Redis event, so downstream
        # consumers never see candidate IDs that were not stored.
        await _resolve_candidate_areas(pool, candidates)
        acquisition_time = _acquisition_datetime(scene_metadata)
        await _persist_candidates(
            pool,
            scene_id,
            acquisition_time,
            candidates,
            is_synthetic=_is_synthetic(scene_metadata),
        )

        # Step 3 Step 8 — one event per scene, only after the DB write
        # succeeds. Follows the existing event semantics: events are emitted
        # when there is output (a scene with zero candidates produces no event).
        if candidates:
            await publish_to_stream(
                redis,
                CANDIDATES_RAW_STREAM,
                _candidates_raw_event(scene_metadata, candidates),
            )

        # The tasking API must expose a scene as fulfilled only after all PNG
        # previews were saved successfully above. This prevents the dashboard
        # from requesting an artifact directory while it is still absent.
        await pool.execute(
            "UPDATE satellite_tasking_requests SET status = 'fulfilled', completed_at = NOW() "
            "WHERE scene_id = $1 AND status = 'processing'",
            scene_id,
        )

        await redis.publish("sar.tasking.events", json.dumps({"scene_id": scene_id, "step": "sar_complete", "candidates": len(candidates)}))

        STATE["scenes_processed"] += 1
        STATE["last_scene_id"] = scene_id
        STATE["last_processed_at"] = datetime.now(timezone.utc).isoformat()
        STATE["last_raster_metadata"] = raster_metadata
        STATE["last_artifact_path"] = artifact_path
        log.info(
            "Processed SAR scene id=%s path=%s shape=%s dark_candidates=%d spill_candidates=%d artifact=%s",
            scene_id,
            raster_path,
            binary_mask.shape,
            int(binary_mask.sum()),
            len(candidates),
            artifact_path,
        )
    except Exception as exc:
        STATE["scenes_failed"] += 1
        if scene_id:
            # Do not leave a tasking request permanently "processing" when
            # artifact generation failed; the vessel UI can then report the
            # failure instead of waiting for previews that will never exist.
            try:
                await pool.execute(
                    "UPDATE satellite_tasking_requests SET status = 'failed', completed_at = NOW() "
                    "WHERE scene_id = $1 AND status = 'processing'",
                    scene_id,
                )
            except Exception:
                log.exception("Could not mark failed SAR tasking for scene=%s", scene_id)
        log.exception(
            "SAR scene processing failed id=%s path=%s: %s",
            scene_id,
            raster_path,
            exc,
        )


async def run_sar_worker() -> None:
    """Consume sar.clean with the shared Redis stream consumer convention."""
    pool = await create_pool()
    redis = await get_redis()
    await ensure_consumer_group(redis, "sar.clean", CONSUMER_GROUP)
    log.info("%s worker started.", SERVICE_NAME)

    while True:
        STATE["heartbeat"] = time.time()
        messages = await consume_stream(
            redis,
            "sar.clean",
            CONSUMER_GROUP,
            CONSUMER_NAME,
            count=10,
            block_ms=2000,
        )
        for message in messages:
            await _process_sar_message(message["data"], pool, redis)
            STATE["messages_consumed"] += 1
