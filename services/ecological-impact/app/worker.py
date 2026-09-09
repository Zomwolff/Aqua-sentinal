"""
Ecological Impact worker — consumes spill.forecast, calculates ecological
exposure for all receptor types and horizons, persists to ecological_impact table.

Phase 4: Ecological Impact V1
- Calculates exposure_pct = (overlap_area / receptor_area) * 100
- Classifies into None/Low/Medium/High/Critical categories
- Tracks time_to_first_exposure for each receptor type
- Handles best_estimate and probability_90 footprints
- Publishes spill.ecological event
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict

sys.path.insert(0, "/app")
from shared.db.connection import create_pool
from shared.redis_client import (
    consume_stream, ensure_consumer_group,
    get_redis, publish_to_stream,
)
from app.exposure import calculate_ecological_impact_for_spill

log = logging.getLogger(__name__)

SERVICE_NAME   = "ecological-impact"
CONSUMER_GROUP = "ecological-impact"
CONSUMER_NAME  = "ei-worker"
INPUT_STREAM   = "spill.forecast"
OUTPUT_STREAM  = "spill.ecological"

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "spills_processed": 0,
    "impacts_written": 0,
    "failed": 0,
    "last_spill_id": None,
}


async def _persist_ecological_impacts(pool, impacts: list[Dict]) -> int:
    """
    Persist ecological impact results to database.
    Idempotent: uses INSERT ... ON CONFLICT DO UPDATE.
    
    Returns: number of records inserted/updated
    """
    count = 0
    for impact in impacts:
        try:
            metadata_json = json.dumps(impact.get("metadata", {}))
            
            await pool.execute(
                """
                INSERT INTO ecological_impact (
                    spill_id, horizon_hours, footprint_type, receptor_type,
                    overlap_area_km2, receptor_area_km2, spill_area_km2,
                    exposure_pct, spill_share_pct, category,
                    sensitivity_tier, protection_status,
                    time_to_first_exposure_hours, metadata, computed_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb, NOW())
                ON CONFLICT (spill_id, horizon_hours, footprint_type, receptor_type)
                DO UPDATE SET
                    overlap_area_km2 = EXCLUDED.overlap_area_km2,
                    receptor_area_km2 = EXCLUDED.receptor_area_km2,
                    spill_area_km2 = EXCLUDED.spill_area_km2,
                    exposure_pct = EXCLUDED.exposure_pct,
                    spill_share_pct = EXCLUDED.spill_share_pct,
                    category = EXCLUDED.category,
                    sensitivity_tier = EXCLUDED.sensitivity_tier,
                    protection_status = EXCLUDED.protection_status,
                    time_to_first_exposure_hours = EXCLUDED.time_to_first_exposure_hours,
                    metadata = EXCLUDED.metadata,
                    computed_at = NOW()
                """,
                impact["spill_id"],
                impact["horizon_hours"],
                impact["footprint_type"],
                impact["receptor_type"],
                impact["overlap_area_km2"],
                impact["receptor_area_km2"],
                impact["spill_area_km2"],
                impact["exposure_pct"],
                impact["spill_share_pct"],
                impact["category"],
                impact.get("sensitivity_tier"),
                impact.get("protection_status"),
                impact.get("time_to_first_exposure_hours"),
                metadata_json
            )
            count += 1
        except Exception as e:
            log.error(
                "Ecological impact insert failed spill=%s horizon=%s receptor=%s: %s",
                impact.get("spill_id"), impact.get("horizon_hours"),
                impact.get("receptor_type"), e
            )
    
    return count


async def _summarize_impacts(impacts: list[Dict]) -> Dict[str, Any]:
    """
    Create summary statistics for publishing to Redis event.
    
    Returns summary with:
        - highest_category: worst category across all receptors/horizons
        - receptors_affected: list of receptor types with exposure > 0
        - total_impacts_calculated: count of result records
        - horizons_covered: list of horizons processed
    """
    if not impacts:
        return {
            "highest_category": "None",
            "receptors_affected": [],
            "total_impacts_calculated": 0,
            "horizons_covered": [],
        }
    
    # Category ordering for "highest"
    category_order = {"None": 0, "Low": 1, "Medium": 2, "High": 3, "Critical": 4}
    
    highest_category = "None"
    receptors_with_exposure = set()
    horizons = set()
    
    for impact in impacts:
        # Track highest category
        cat = impact["category"]
        if category_order.get(cat, 0) > category_order.get(highest_category, 0):
            highest_category = cat
        
        # Track receptors with exposure
        if impact["exposure_pct"] > 0:
            receptors_with_exposure.add(impact["receptor_type"])
        
        # Track horizons
        horizons.add(impact["horizon_hours"])
    
    return {
        "highest_category": highest_category,
        "receptors_affected": sorted(list(receptors_with_exposure)),
        "total_impacts_calculated": len(impacts),
        "horizons_covered": sorted(list(horizons)),
    }


async def _process_spill_forecast(
    data: Dict[str, Any], pool, redis,
) -> None:
    """
    Process a spill.forecast event: calculate ecological impact, persist, publish.
    """
    spill_id = str(data.get("spill_id") or "")
    if not spill_id:
        log.warning("spill.forecast message missing spill_id")
        return
    
    forecast_count = int(data.get("forecast_count", 0))
    max_horizon = data.get("max_horizon_hours", 24)
    
    log.info(
        "Calculating ecological impact: spill=%s forecasts=%d max_horizon=%sh",
        spill_id, forecast_count, max_horizon
    )
    
    try:
        # Calculate ecological impact for all horizons and receptor types
        impacts = await calculate_ecological_impact_for_spill(pool, spill_id)
        
        if not impacts:
            log.warning("No ecological impacts calculated for spill=%s (no forecasts?)", spill_id)
            return
        
        # Persist to database (idempotent)
        written = await _persist_ecological_impacts(pool, impacts)
        STATE["impacts_written"] += written
        
        # Summarize for event
        summary = await _summarize_impacts(impacts)
        
        log.info(
            "Ecological impact: spill=%s impacts=%d written=%d highest=%s receptors=%s",
            spill_id, len(impacts), written,
            summary["highest_category"],
            ",".join(summary["receptors_affected"]) if summary["receptors_affected"] else "none"
        )
        
        # Publish spill.ecological event
        await publish_to_stream(redis, OUTPUT_STREAM, {
            "spill_id": spill_id,
            "impacts_calculated": len(impacts),
            "highest_category": summary["highest_category"],
            "receptors_affected": summary["receptors_affected"],
            "horizons_covered": summary["horizons_covered"],
            "computed_at": datetime.now(timezone.utc).isoformat(),
        })
        
        STATE["last_spill_id"] = spill_id
        STATE["spills_processed"] += 1
        
    except Exception as exc:
        STATE["failed"] += 1
        log.exception("Ecological impact calculation failed for spill=%s: %s", spill_id, exc)
        raise


async def run_ecological_worker() -> None:
    """Main worker loop."""
    # Debug: print DSN
    from shared.db.connection import get_dsn
    dsn = get_dsn()
    log.info(f"Connecting with DSN: {dsn.replace(os.environ.get('POSTGRES_PASSWORD', 'XXX'), 'XXX')}")
    
    pool  = await create_pool()
    redis = await get_redis()
    await ensure_consumer_group(redis, INPUT_STREAM, CONSUMER_GROUP)
    
    log.info("%s worker started.", SERVICE_NAME)
    
    while True:
        STATE["heartbeat"] = time.time()
        messages = await consume_stream(
            redis, INPUT_STREAM, CONSUMER_GROUP, CONSUMER_NAME,
            count=10, block_ms=2000,
        )
        
        for msg in messages:
            try:
                await _process_spill_forecast(msg["data"], pool, redis)
            except Exception as exc:
                log.exception("Ecological worker failed msg=%s: %s", msg.get("id"), exc)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    asyncio.run(run_ecological_worker())
