"""
Origin Inference Worker — origin_worker.py

Consumes the `origin.infer` Redis stream and runs the existing inference
pipeline for each queued job.

All science is delegated to origin_service.run_origin_inference().
This file contains only job lifecycle management.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from shared.redis_client import (
    consume_stream,
    ensure_consumer_group,
    get_redis,
    hget_json,
    hset_json,
)
from shared.db.connection import get_pool
from app.origin_api import INFER_STREAM, _job_key, _JOB_TTL_S, serialize_origin_result

log = logging.getLogger(__name__)

_CONSUMER_GROUP = "origin-infer"
_CONSUMER_NAME  = "origin-worker"


async def _process_infer_job(job_id: str, redis) -> None:
    """Load, execute, and persist one origin inference job."""

    # Mark running immediately so the client can see progress
    await hset_json(redis, _job_key(job_id), {"status": "running"})
    await redis.expire(_job_key(job_id), _JOB_TTL_S)

    raw = await hget_json(redis, _job_key(job_id))
    if not raw:
        log.error("Origin job %s disappeared from Redis before processing", job_id)
        return

    try:
        detection_time = datetime.fromisoformat(raw["detection_time"])
        # hget_json already decodes JSON hash values.  Keep compatibility with
        # pre-existing jobs written before that helper was used.
        stored_footprint = raw["observed_footprint"]
        footprint_dict = (
            json.loads(stored_footprint)
            if isinstance(stored_footprint, str)
            else stored_footprint
        )

        # Reconstruct footprint Pydantic model
        from app.schemas import PolygonGeometry, MultiPolygonGeometry
        geom_type = footprint_dict.get("type")
        if geom_type == "Polygon":
            footprint = PolygonGeometry(**footprint_dict)
        elif geom_type == "MultiPolygon":
            footprint = MultiPolygonGeometry(**footprint_dict)
        else:
            raise ValueError(f"Unsupported geometry type: {geom_type!r}")

        # Delegate entirely to the production service layer.
        from app.origin_service import run_origin_inference
        t0 = time.time()
        origin_result = await run_origin_inference(
            detection_time=detection_time,
            observed_footprint=footprint,
            forcing_pool=get_pool(),
        )
        elapsed = time.time() - t0

        result_json = json.dumps(serialize_origin_result(origin_result), default=str)

        await hset_json(redis, _job_key(job_id), {
            "status": "completed",
            "result": result_json,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": str(round(elapsed, 1)),
        })
        await redis.expire(_job_key(job_id), _JOB_TTL_S)
        log.info("Origin job completed: job_id=%s elapsed=%.1fs", job_id, elapsed)

    except Exception as exc:
        log.exception("Origin job failed: job_id=%s", job_id)
        await hset_json(redis, _job_key(job_id), {
            "status": "failed",
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:500],
            "failed_at": datetime.now(timezone.utc).isoformat(),
        })
        await redis.expire(_job_key(job_id), _JOB_TTL_S)


async def run_origin_worker() -> None:
    """Async loop — consumes origin.infer stream until cancelled."""
    redis = await get_redis()
    await ensure_consumer_group(redis, INFER_STREAM, _CONSUMER_GROUP)
    log.info("Origin inference worker started.")

    while True:
        messages = await consume_stream(
            redis, INFER_STREAM, _CONSUMER_GROUP, _CONSUMER_NAME,
            count=5, block_ms=2000,
        )
        for msg in messages:
            job_id = msg["data"].get("job_id", "")
            if not job_id:
                log.warning("origin.infer message missing job_id: %s", msg)
                continue
            try:
                await _process_infer_job(job_id, redis)
            except Exception as exc:
                log.exception(
                    "Unexpected error processing origin job %s: %s", job_id, exc
                )
