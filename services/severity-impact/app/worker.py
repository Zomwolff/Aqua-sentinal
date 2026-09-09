"""
Severity Impact V1 worker — consumes spill.ecological, queries forecasts +
ecological_impact + protected_areas, computes horizon-aware rule-based severity,
persists to severity table, publishes to spill.severity.

V1 Methodology:
- Dominant consequence (no weighted formula)
- Ecological categories inherited from Ecological Impact V1
- Socioeconomic based on port/fishing zone proximity only
- Rule-based escalation (breadth, cross-domain, TTFE)
- Score [0-100] with fixed bands (secondary to severity_level)
- Horizon processing: current, 1h, 3h, 6h, 12h, 24h
- Footprint processing: best_estimate, probability_90
"""
from __future__ import annotations
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/app")
from shared.db.connection import create_pool
from shared.redis_client import (
    consume_stream, ensure_consumer_group,
    get_redis, publish_to_stream,
)
from app.severity import (
    determine_ecological_severity,
    determine_socioeconomic_severity,
    apply_escalation_rules,
    calculate_severity_score,
    calculate_urgency_factor,
    determine_trajectory,
    max_severity,
    compare_severity,
    normalize_severity,
    to_db_severity,  # For DB/Redis writes
)

log = logging.getLogger(__name__)

SERVICE_NAME   = "severity-impact"
CONSUMER_GROUP = "severity-impact"
CONSUMER_NAME  = "si-worker-v1"
INPUT_STREAM   = "spill.ecological"  # Changed from spill.attributed
OUTPUT_STREAM  = "spill.severity"
METHODOLOGY_VERSION = "severity-impact-v1"

# Standard forecast horizons
HORIZONS = [1.0, 3.0, 6.0, 12.0, 24.0]
FOOTPRINT_TYPES = ["best_estimate", "probability_90"]
RECEPTOR_TYPES = ["mangrove", "coral_reef", "mpa", "sensitive_coastline"]

STATE: Dict[str, Any] = {
    "heartbeat": None,
    "spills_processed": 0,
    "failed": 0,
    "last_spill_id": None,
}


def _parse_float(val, default=0.0):
    try:
        return float(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


async def _fetch_forecasts(pool, spill_id: str) -> List[Dict]:
    """Fetch all forecast horizons with relevant metrics."""
    rows = await pool.fetch(
        """
        SELECT 
            horizon_hours,
            ST_AsText(geom) AS geom_wkt,
            ST_AsText(probability_90_geom) AS prob90_wkt,
            physical_area_m2,
            drift_distance_m,
            expansion_ratio,
            spread_rate_m2_per_hour,
            confidence
        FROM forecasts
        WHERE spill_id = $1
        ORDER BY horizon_hours ASC
        """,
        spill_id
    )
    
    return [dict(r) for r in rows]


async def _fetch_ecological_impacts(pool, spill_id: str) -> List[Dict]:
    """Fetch all ecological impact assessments."""
    rows = await pool.fetch(
        """
        SELECT 
            horizon_hours,
            footprint_type,
            receptor_type,
            category,
            exposure_pct,
            time_to_first_exposure_hours,
            sensitivity_tier
        FROM ecological_impact
        WHERE spill_id = $1
        ORDER BY horizon_hours, footprint_type, receptor_type
        """,
        spill_id
    )
    
    return [dict(r) for r in rows]


async def _fetch_socioeconomic_proximity(
    pool, footprint_wkt: str
) -> tuple[int, int]:
    """
    Query port and fishing zone proximity to forecast footprint.
    
    Returns: (ports_within_5km, fishing_zones_within_10km)
    """
    # Ports within 5km
    port_count = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM protected_areas
        WHERE area_type = 'port'
          AND ST_DWithin(
              geom::geography,
              ST_SetSRID(ST_GeomFromText($1), 4326)::geography,
              5000
          )
        """,
        footprint_wkt
    )
    
    # Fishing zones within 10km
    fishing_count = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM protected_areas
        WHERE area_type = 'fishing_zone'
          AND ST_DWithin(
              geom::geography,
              ST_SetSRID(ST_GeomFromText($1), 4326)::geography,
              10000
          )
        """,
        footprint_wkt
    )
    
    return int(port_count or 0), int(fishing_count or 0)


async def _fetch_current_spill_geometry(pool, spill_id: str) -> Optional[str]:
    """Fetch current spill geometry from spill_incidents."""
    row = await pool.fetchrow(
        "SELECT ST_AsText(geom) AS geom_wkt FROM spill_incidents WHERE id = $1",
        spill_id
    )
    return row["geom_wkt"] if row else None


async def _calculate_severity_for_horizon(
    pool,
    spill_id: str,
    horizon_hours: Optional[float],
    footprint_type: str,
    footprint_wkt: str,
    ecological_impacts: List[Dict],
    physical_area_m2: Optional[float],
    drift_distance_m: Optional[float],
    expansion_ratio: Optional[float],
) -> Dict:
    """
    Calculate severity for a single horizon/footprint combination.
    """
    # Filter ecological impacts for this horizon/footprint
    impacts_for_horizon = [
        imp for imp in ecological_impacts
        if (imp.get("horizon_hours") == horizon_hours or (horizon_hours is None and imp.get("horizon_hours") == 1.0))
        and imp.get("footprint_type") == footprint_type
    ]
    
    # Ecological severity (maximum receptor category)
    eco_severity, high_crit_receptors, affected_count = determine_ecological_severity(impacts_for_horizon)
    
    # Socioeconomic severity (port/fishing zone proximity)
    ports, fishing_zones = await _fetch_socioeconomic_proximity(pool, footprint_wkt)
    socio_severity, socio_drivers = determine_socioeconomic_severity(ports, fishing_zones)
    
    # Base severity = maximum of ecological and socioeconomic
    base_severity = max_severity([eco_severity, socio_severity])
    
    # TTFE (from ecological impacts)
    ttfe_hours = None
    for imp in impacts_for_horizon:
        if imp.get("time_to_first_exposure_hours") is not None:
            if ttfe_hours is None:
                ttfe_hours = imp["time_to_first_exposure_hours"]
            else:
                ttfe_hours = min(ttfe_hours, imp["time_to_first_exposure_hours"])
    
    # Apply escalation rules
    final_severity, escalation_applied, escalation_reasons = apply_escalation_rules(
        base_severity,
        eco_severity,
        socio_severity,
        len(high_crit_receptors),
        ttfe_hours
    )
    
    # Calculate score [0-100]
    max_exposure_pct = 0.0
    for imp in impacts_for_horizon:
        max_exposure_pct = max(max_exposure_pct, float(imp.get("exposure_pct", 0.0)))
    
    exposure_intensity = min(1.0, max_exposure_pct / 100.0)
    breadth_ratio = affected_count / len(RECEPTOR_TYPES) if affected_count > 0 else 0.0
    urgency_factor = calculate_urgency_factor(ttfe_hours)
    
    score = calculate_severity_score(
        final_severity,
        exposure_intensity,
        breadth_ratio,
        urgency_factor
    )
    
    # Build primary drivers
    drivers = []
    
    # Ecological drivers
    for receptor in high_crit_receptors:
        drivers.append(f"{receptor}_high_critical_exposure")
    
    # Socioeconomic drivers
    drivers.extend(socio_drivers)
    
    # Escalation drivers
    if escalation_applied:
        drivers.extend(escalation_reasons)
    
    # TTFE driver
    if ttfe_hours is not None and ttfe_hours < 3.0:
        drivers.append(f"ttfe_{ttfe_hours:.1f}h")
    
    return {
        "severity_level": final_severity,
        "score": score,
        "ecological_severity": eco_severity,
        "socioeconomic_severity": socio_severity,
        "escalation_applied": escalation_applied,
        "escalation_reasons": escalation_reasons,
        "primary_drivers": drivers,
        "ttfe_hours": ttfe_hours,
        "affected_receptor_count": affected_count,
        "ports_within_5km": ports,
        "fishing_zones_within_10km": fishing_zones,
        "physical_area_m2": physical_area_m2,
        "drift_distance_m": drift_distance_m,
        "expansion_ratio": expansion_ratio,
    }


async def _process_spill_ecological(
    data: Dict[str, Any], pool, redis,
) -> None:
    """Process spill.ecological event and calculate V1 severity."""
    spill_id = str(data.get("spill_id") or "")
    
    if not spill_id:
        log.warning("spill.ecological missing spill_id")
        return
    
    log.info("Processing Severity-Impact V1 for spill=%s", spill_id)
    
    # Fetch dependencies
    forecasts = await _fetch_forecasts(pool, spill_id)
    ecological_impacts = await _fetch_ecological_impacts(pool, spill_id)

    if not forecasts:
        log.warning("No forecasts found for spill=%s, skipping severity calculation", spill_id)
        return

    if not ecological_impacts:
        log.warning("No ecological impacts found for spill=%s, skipping severity calculation", spill_id)
        return
    
    # Calculate severity for all horizon/footprint combinations
    all_results = []
    
    for forecast in forecasts:
        horizon_hours = float(forecast["horizon_hours"])
        
        for footprint_type in FOOTPRINT_TYPES:
            # Get geometry (with fallback)
            if footprint_type == "probability_90":
                footprint_wkt = forecast.get("prob90_wkt")
                if not footprint_wkt:
                    # Fallback to best_estimate
                    footprint_wkt = forecast.get("geom_wkt")
            else:
                footprint_wkt = forecast.get("geom_wkt")
            
            if not footprint_wkt:
                continue
            
            try:
                result = await _calculate_severity_for_horizon(
                    pool, spill_id, horizon_hours, footprint_type, footprint_wkt,
                    ecological_impacts,
                    forecast.get("physical_area_m2"),
                    forecast.get("drift_distance_m"),
                    forecast.get("expansion_ratio"),
                )
                
                result["horizon_hours"] = horizon_hours
                result["footprint_type"] = footprint_type
                all_results.append(result)
                
            except Exception as e:
                log.error("Failed to calculate severity for spill=%s horizon=%s footprint=%s: %s",
                          spill_id, horizon_hours, footprint_type, e)
    
    if not all_results:
        log.error("No severity results calculated for spill=%s", spill_id)
        return

    # Determine overall severity (V1 methodology: 1h probability_90 as current baseline).
    # Fallback chain: 1h p90 → any p90 → first available result.
    overall_result = None
    for r in all_results:
        if r.get("horizon_hours") == 1.0 and r.get("footprint_type") == "probability_90":
            overall_result = r
            break
    if not overall_result:
        for r in all_results:
            if r.get("footprint_type") == "probability_90":
                overall_result = r
                break
    if not overall_result:
        overall_result = all_results[0]

    # Determine peak severity across all horizons/footprints.
    peak_result = max(all_results, key=lambda r: (
        ["None", "Low", "Medium", "High", "Critical"].index(normalize_severity(r["severity_level"])),
        r["score"]
    ))

    # Determine trajectory: 1h probability_90 vs 24h probability_90 (V1 methodology).
    # Named variables make the comparison explicit and prevent regression to any other baseline.
    p90_1h  = next((r for r in all_results
                    if r.get("horizon_hours") == 1.0
                    and r.get("footprint_type") == "probability_90"), None)
    p90_24h = next((r for r in all_results
                    if r.get("horizon_hours") == 24.0
                    and r.get("footprint_type") == "probability_90"), None)
    trajectory = "STABLE"
    if p90_1h and p90_24h:
        trajectory = determine_trajectory(p90_1h["severity_level"], p90_24h["severity_level"])
    
    # Persist overall severity record
    try:
        await pool.execute(
            """
            INSERT INTO severity (
                spill_id, severity_level, score,
                horizon_hours, footprint_type, methodology_version,
                peak_severity, peak_horizon_hours, peak_footprint_type, peak_score,
                trajectory,
                ecological_severity, socioeconomic_severity,
                escalation_applied, escalation_reasons,
                primary_drivers,
                ttfe_hours, affected_receptor_count,
                ports_within_5km, fishing_zones_within_10km,
                physical_area_m2, drift_distance_m, expansion_ratio,
                environmental_risk, population_risk, economic_risk, protected_area_risk,
                computed_at
            ) VALUES (
                $1, $2::severity_level_enum, $3,
                NULL, 'current', $4,
                $5::severity_level_enum, $6, $7, $8,
                $9,
                $10, $11,
                $12, $13,
                $14::jsonb,
                $15, $16,
                $17, $18,
                $19, $20, $21,
                0.0, 0.0, 0.0, 0.0,
                NOW()
            )
            ON CONFLICT (spill_id, COALESCE(horizon_hours, 0), COALESCE(footprint_type, 'current'))
            DO UPDATE SET
                severity_level = EXCLUDED.severity_level,
                score = EXCLUDED.score,
                methodology_version = EXCLUDED.methodology_version,
                peak_severity = EXCLUDED.peak_severity,
                peak_horizon_hours = EXCLUDED.peak_horizon_hours,
                peak_footprint_type = EXCLUDED.peak_footprint_type,
                peak_score = EXCLUDED.peak_score,
                trajectory = EXCLUDED.trajectory,
                ecological_severity = EXCLUDED.ecological_severity,
                socioeconomic_severity = EXCLUDED.socioeconomic_severity,
                escalation_applied = EXCLUDED.escalation_applied,
                escalation_reasons = EXCLUDED.escalation_reasons,
                primary_drivers = EXCLUDED.primary_drivers,
                ttfe_hours = EXCLUDED.ttfe_hours,
                affected_receptor_count = EXCLUDED.affected_receptor_count,
                ports_within_5km = EXCLUDED.ports_within_5km,
                fishing_zones_within_10km = EXCLUDED.fishing_zones_within_10km,
                physical_area_m2 = EXCLUDED.physical_area_m2,
                drift_distance_m = EXCLUDED.drift_distance_m,
                expansion_ratio = EXCLUDED.expansion_ratio,
                computed_at = NOW()
            """,
            spill_id,
            to_db_severity(overall_result["severity_level"]),  # Use to_db_severity for DB enum
            overall_result["score"],
            METHODOLOGY_VERSION,
            to_db_severity(peak_result["severity_level"]),  # Use to_db_severity for DB enum
            peak_result.get("horizon_hours"),
            peak_result.get("footprint_type"),
            peak_result["score"],
            trajectory,
            overall_result["ecological_severity"],
            overall_result["socioeconomic_severity"],
            overall_result["escalation_applied"],
            overall_result["escalation_reasons"],
            json.dumps(overall_result["primary_drivers"]),
            overall_result.get("ttfe_hours"),
            overall_result.get("affected_receptor_count", 0),
            overall_result.get("ports_within_5km", 0),
            overall_result.get("fishing_zones_within_10km", 0),
            overall_result.get("physical_area_m2"),
            overall_result.get("drift_distance_m"),
            overall_result.get("expansion_ratio"),
        )
    except Exception as e:
        log.error("Failed to insert overall severity for spill=%s: %s", spill_id, e)
        raise
    
    # Persist detailed horizon/footprint records
    for result in all_results:
        try:
            await pool.execute(
                """
                INSERT INTO severity (
                    spill_id, severity_level, score,
                    horizon_hours, footprint_type, methodology_version,
                    ecological_severity, socioeconomic_severity,
                    escalation_applied, escalation_reasons,
                    primary_drivers,
                    ttfe_hours, affected_receptor_count,
                    ports_within_5km, fishing_zones_within_10km,
                    physical_area_m2, drift_distance_m, expansion_ratio,
                    environmental_risk, population_risk, economic_risk, protected_area_risk,
                    computed_at
                ) VALUES (
                    $1, $2::severity_level_enum, $3,
                    $4, $5, $6,
                    $7, $8,
                    $9, $10,
                    $11::jsonb,
                    $12, $13,
                    $14, $15,
                    $16, $17, $18,
                    0.0, 0.0, 0.0, 0.0,
                    NOW()
                )
                ON CONFLICT (spill_id, COALESCE(horizon_hours, 0), COALESCE(footprint_type, 'current'))
                DO UPDATE SET
                    severity_level = EXCLUDED.severity_level,
                    score = EXCLUDED.score,
                    methodology_version = EXCLUDED.methodology_version,
                    ecological_severity = EXCLUDED.ecological_severity,
                    socioeconomic_severity = EXCLUDED.socioeconomic_severity,
                    escalation_applied = EXCLUDED.escalation_applied,
                    escalation_reasons = EXCLUDED.escalation_reasons,
                    primary_drivers = EXCLUDED.primary_drivers,
                    ttfe_hours = EXCLUDED.ttfe_hours,
                    affected_receptor_count = EXCLUDED.affected_receptor_count,
                    ports_within_5km = EXCLUDED.ports_within_5km,
                    fishing_zones_within_10km = EXCLUDED.fishing_zones_within_10km,
                    physical_area_m2 = EXCLUDED.physical_area_m2,
                    drift_distance_m = EXCLUDED.drift_distance_m,
                    expansion_ratio = EXCLUDED.expansion_ratio,
                    computed_at = NOW()
                """,
                spill_id,
                to_db_severity(result["severity_level"]),  # Use to_db_severity for DB enum
                result["score"],
                result["horizon_hours"],
                result["footprint_type"],
                METHODOLOGY_VERSION,
                result["ecological_severity"],
                result["socioeconomic_severity"],
                result["escalation_applied"],
                result["escalation_reasons"],
                json.dumps(result["primary_drivers"]),
                result.get("ttfe_hours"),
                result.get("affected_receptor_count", 0),
                result.get("ports_within_5km", 0),
                result.get("fishing_zones_within_10km", 0),
                result.get("physical_area_m2"),
                result.get("drift_distance_m"),
                result.get("expansion_ratio"),
            )
        except Exception as e:
            log.error("Failed to insert horizon severity for spill=%s h=%s ft=%s: %s",
                      spill_id, result.get("horizon_hours"), result.get("footprint_type"), e)
    
    # Fetch spill coordinates for Response-Decision compatibility
    spill_row = await pool.fetchrow(
        "SELECT latitude, longitude FROM spill_incidents WHERE id = $1",
        spill_id
    )
    spill_lat = float(spill_row["latitude"]) if spill_row else 0.0
    spill_lon = float(spill_row["longitude"]) if spill_row else 0.0
    
    # Publish spill.severity event (Response-Decision compatible + V1 extensions)
    await publish_to_stream(redis, OUTPUT_STREAM, {
        # Response-Decision required fields
        "spill_id": spill_id,
        "spill_lat": spill_lat,
        "spill_lon": spill_lon,
        "severity_level": to_db_severity(overall_result["severity_level"]),  # LOW/MODERATE/HIGH/CRITICAL
        "severity_score": overall_result["score"],  # [0-100]
        "protected_area_risk": min(1.0, overall_result.get("ports_within_5km", 0) * 0.5),  # Proxy [0-1]
        "population_risk": 0.0,  # V1: no population data (kept for compatibility)
        "protected_areas_nearby": overall_result.get("ports_within_5km", 0) + overall_result.get("fishing_zones_within_10km", 0),
        "coast_distance_m": 0.0,  # V1: not used (kept for compatibility)
        "is_synthetic": str(data.get("is_synthetic", "false")),
        
        # V1 extensions
        "peak_severity": to_db_severity(peak_result["severity_level"]),  # LOW/MODERATE/HIGH/CRITICAL
        "peak_horizon_hours": peak_result.get("horizon_hours"),
        "peak_footprint_type": peak_result.get("footprint_type"),
        "trajectory": trajectory,
        "methodology_version": METHODOLOGY_VERSION,
        "ecological_severity": overall_result["ecological_severity"],
        "socioeconomic_severity": overall_result["socioeconomic_severity"],
        "escalation_applied": overall_result["escalation_applied"],
        "primary_drivers": overall_result["primary_drivers"],
    })
    
    STATE["spills_processed"] += 1
    STATE["last_spill_id"] = spill_id
    
    log.info(
        "Severity V1: spill=%s overall=%s score=%.1f peak=%s@%sh eco=%s socio=%s trajectory=%s",
        spill_id,
        overall_result["severity_level"],
        overall_result["score"],
        peak_result["severity_level"],
        peak_result.get("horizon_hours", 0),
        overall_result["ecological_severity"],
        overall_result["socioeconomic_severity"],
        trajectory,
    )


async def run_severity_worker() -> None:
    pool  = await create_pool()
    redis = await get_redis()
    await ensure_consumer_group(redis, INPUT_STREAM, CONSUMER_GROUP)
    log.info("%s worker started (V1).", SERVICE_NAME)

    while True:
        STATE["heartbeat"] = time.time()
        messages = await consume_stream(
            redis, INPUT_STREAM, CONSUMER_GROUP, CONSUMER_NAME,
            count=10, block_ms=0,
        )
        if not messages:
            await asyncio.sleep(0.1)
            continue
        for msg in messages:
            try:
                await _process_spill_ecological(msg["data"], pool, redis)
            except Exception as exc:
                STATE["failed"] += 1
                log.exception("Severity V1 failed msg=%s: %s", msg.get("id"), exc)

