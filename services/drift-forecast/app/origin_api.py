"""
Origin Inference API Router — origin_api.py

Exposes:
  POST /api/v1/origin/infer          — submit async inference job (HTTP 202)
  GET  /api/v1/origin/jobs/{job_id}  — poll job status and retrieve result

Routing and serialization only.  All inference runs in the worker.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from shared.redis_client import get_redis, hset_json, hget_json, publish_to_stream
from app.schemas import (
    InferOriginRequest,
    JobAcceptedResponse,
    JobStatusResponse,
    OriginResultOut,
    SpatialPosteriorOut,
    SpatialCredibleRegionOut,
    PosteriorCandidateOut,
    TemporalPosteriorOut,
    PosteriorDiagnosticsOut,
    ProvenanceOut,
    ErrorDetail,
    _geom_centroid_and_area,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/origin", tags=["Origin Inference"])

# Redis stream consumed by the origin worker
INFER_STREAM = "origin.infer"

# Redis key prefix for job state hashes
_JOB_KEY_PREFIX = "inference:job:"

# Job TTL: 24 h
_JOB_TTL_S = 86_400


def _job_key(job_id: str) -> str:
    return f"{_JOB_KEY_PREFIX}{job_id}"


# ── POST /api/v1/origin/infer ──────────────────────────────────────────────────

@router.post(
    "/infer",
    status_code=202,
    response_model=JobAcceptedResponse,
    summary="Submit origin inference job",
    description=(
        "Accepts an observed spill footprint and queues an asynchronous "
        "origin inference job.  Returns HTTP 202 immediately with a job_id.  "
        "Poll GET /api/v1/origin/jobs/{job_id} for status and results."
    ),
    responses={
        202: {"description": "Job accepted and queued"},
        422: {"description": "Validation error — invalid geometry or detection_time"},
    },
)
async def infer_origin(request: InferOriginRequest) -> JobAcceptedResponse:
    # Validate footprint area before accepting the job
    try:
        _, area_m2 = _geom_centroid_and_area(request.observed_footprint)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid footprint geometry: {exc}")

    if area_m2 <= 0:
        raise HTTPException(
            status_code=422,
            detail="observed_footprint has zero or negative area — cannot perform inference",
        )

    redis = await get_redis()
    job_id = str(uuid.uuid4())

    # Store initial job state in Redis hash
    job_state: Dict[str, str] = {
        "job_id": job_id,
        "status": "queued",
        "detection_time": request.detection_time.isoformat(),
        "observed_footprint": request.observed_footprint.model_dump_json(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await hset_json(redis, _job_key(job_id), job_state)
    await redis.expire(_job_key(job_id), _JOB_TTL_S)

    # Enqueue on origin.infer stream — worker picks this up
    await publish_to_stream(redis, INFER_STREAM, {"job_id": job_id})

    log.info("Origin inference job queued: job_id=%s", job_id)
    return JobAcceptedResponse(job_id=job_id)


# ── GET /api/v1/origin/jobs/{job_id} ──────────────────────────────────────────

@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    summary="Get origin inference job status",
    description=(
        "Returns the current status of an origin inference job.  "
        "Status transitions: queued → running → completed | failed."
    ),
    responses={
        200: {"description": "Job status (queued / running / completed / failed)"},
        404: {"description": "Job not found"},
    },
)
async def get_job_status(job_id: str) -> JobStatusResponse:
    redis = await get_redis()
    raw = await hget_json(redis, _job_key(job_id))

    if not raw:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    status = raw.get("status", "unknown")

    if status == "completed":
        result_json = raw.get("result")
        result = _deserialize_result(result_json) if result_json else None
        return JobStatusResponse(job_id=job_id, status="completed", result=result)

    if status == "failed":
        return JobStatusResponse(
            job_id=job_id,
            status="failed",
            error=ErrorDetail(
                code=raw.get("error_code", "INFERENCE_FAILED"),
                message=raw.get("error_message", "Unknown error"),
            ),
        )

    # queued or running
    return JobStatusResponse(job_id=job_id, status=status)  # type: ignore[arg-type]


# ── Serialization helpers ──────────────────────────────────────────────────────

def serialize_origin_result(origin_result: Any) -> Dict[str, Any]:
    """
    Convert an OriginProbabilityResult to a plain dict for Redis / JSON.
    Only serializes — does not compute any new values.
    """
    sp = origin_result.spatial_posterior
    tp = origin_result.temporal_posterior
    d = origin_result.diagnostics

    def _dt(dt: Any) -> str:
        return dt.isoformat() if dt is not None else ""

    def _candidate(c: Any) -> Dict[str, Any]:
        return {
            "candidate_id": c.candidate_id,
            "source_lat": c.source_lat,
            "source_lon": c.source_lon,
            "release_time": _dt(c.release_time),
            "posterior_weight": c.posterior_weight,
        }

    def _credible_region(cr: Any) -> Dict[str, Any]:
        return {
            "level": cr.level,
            "cumulative_weight": cr.cumulative_weight,
            "n_candidates": cr.n_candidates,
            "spatial_extent_km": cr.spatial_extent_km,
            "candidates": [_candidate(c) for c in cr.candidates],
            "hull_wkt": cr.hull_wkt,
        }

    return {
        "origin": {
            "map_lat": sp.map_lat,
            "map_lon": sp.map_lon,
            "map_release_time": _dt(tp.map_release_time),
        },
        "spatial_posterior": {
            "map_lat": sp.map_lat,
            "map_lon": sp.map_lon,
            "weighted_centroid_lat": sp.weighted_centroid_lat,
            "weighted_centroid_lon": sp.weighted_centroid_lon,
            "weighted_std_lat_m": sp.weighted_std_lat_m,
            "weighted_std_lon_m": sp.weighted_std_lon_m,
            "weighted_rms_spread_m": sp.weighted_rms_spread_m,
            "credible_50": _credible_region(sp.credible_50),
            "credible_90": _credible_region(sp.credible_90),
        },
        "temporal_posterior": {
            "map_release_time": _dt(tp.map_release_time),
            "weighted_mean_release_time": _dt(tp.weighted_mean_release_time),
            "credible_50_lo": _dt(tp.credible_50_lo),
            "credible_50_hi": _dt(tp.credible_50_hi),
            "credible_90_lo": _dt(tp.credible_90_lo),
            "credible_90_hi": _dt(tp.credible_90_hi),
            "spread_hours": tp.spread_hours,
            "distribution": [(_dt(t), float(w)) for t, w in tp.distribution],
        },
        "diagnostics": {
            "ess": d.ess,
            "ess_ratio": d.ess_ratio,
            "max_posterior_weight": d.max_posterior_weight,
            "n_total_candidates": d.n_total_candidates,
            "n_nonzero_weight_candidates": d.n_nonzero_weight_candidates,
            "n_significant_candidates": d.n_significant_candidates,
            "n_unique_release_times": d.n_unique_release_times,
            "candidate_spatial_extent_km": d.candidate_spatial_extent_km,
            "candidate_time_extent_h": d.candidate_time_extent_h,
            "quality_flags": list(d.quality_flags),
            "warnings": list(d.warnings),
        },
        "provenance": {
            "detection_time": _dt(origin_result.detection_time),
            "generation_method": origin_result.generation_method,
            "inference_status": origin_result.inference_status,
            "n_forcing_samples": origin_result.n_forcing_samples,
            "model_info": dict(origin_result.provenance or {}),
        },
    }


def _deserialize_result(result_json: Any) -> OriginResultOut:
    """Reconstruct OriginResultOut from stored JSON string or dict."""
    data: Dict[str, Any] = (
        json.loads(result_json) if isinstance(result_json, str) else result_json
    )

    def _cr(cr_data: Dict[str, Any]) -> SpatialCredibleRegionOut:
        return SpatialCredibleRegionOut(
            level=cr_data["level"],
            cumulative_weight=cr_data["cumulative_weight"],
            n_candidates=cr_data["n_candidates"],
            spatial_extent_km=cr_data["spatial_extent_km"],
            candidates=[PosteriorCandidateOut(**c) for c in cr_data["candidates"]],
            hull_wkt=cr_data.get("hull_wkt"),
        )

    sp = data["spatial_posterior"]
    tp = data["temporal_posterior"]
    dg = data["diagnostics"]
    pv = data["provenance"]

    return OriginResultOut(
        origin=data["origin"],
        spatial_posterior=SpatialPosteriorOut(
            map_lat=sp["map_lat"],
            map_lon=sp["map_lon"],
            weighted_centroid_lat=sp["weighted_centroid_lat"],
            weighted_centroid_lon=sp["weighted_centroid_lon"],
            weighted_std_lat_m=sp["weighted_std_lat_m"],
            weighted_std_lon_m=sp["weighted_std_lon_m"],
            weighted_rms_spread_m=sp["weighted_rms_spread_m"],
            credible_50=_cr(sp["credible_50"]),
            credible_90=_cr(sp["credible_90"]),
        ),
        temporal_posterior=TemporalPosteriorOut(**tp),
        diagnostics=PosteriorDiagnosticsOut(**dg),
        provenance=ProvenanceOut(**pv),
    )
