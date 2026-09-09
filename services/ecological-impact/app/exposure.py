"""
exposure.py
===========
Core ecological exposure calculation functions for Aqua-Sentinel Phase 4.

Implements:
- Spatial intersection between forecast geometries and ecological receptors
- Exposure percentage calculation (overlap_area / receptor_area * 100)
- Spill-share percentage calculation (overlap_area / spill_area * 100)
- Exposure category classification (None/Low/Medium/High/Critical)
- Time-to-first-exposure derivation

METHODOLOGY:
- Uses PostGIS for spatial operations: ST_Intersects, ST_Intersection, ST_Area
- Follows project conventions: EPSG:4326 geometry, ::geography for metric areas
- Aggregates areas across multiple receptor geometries BEFORE percentage calc
- Does NOT weight by sensitivity or combine receptor types
"""
from __future__ import annotations

from typing import Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# EXPOSURE CLASSIFICATION [PROJECT-DEFINED]
# ─────────────────────────────────────────────────────────────────────────────

def classify_exposure(exposure_pct: float) -> str:
    """
    Classify exposure percentage into operational categories.
    
    [PROJECT-DEFINED] Thresholds:
        0%         → None
        >0–10%     → Low
        >10–30%    → Medium
        >30–60%    → High
        >60%       → Critical
    
    These are operational thresholds for V1 oil-spill response prioritization,
    NOT universal scientific oil-spill impact thresholds.
    """
    if exposure_pct == 0:
        return "None"
    elif exposure_pct <= 10:
        return "Low"
    elif exposure_pct <= 30:
        return "Medium"
    elif exposure_pct <= 60:
        return "High"
    else:
        return "Critical"


# ─────────────────────────────────────────────────────────────────────────────
# SPATIAL INTERSECTION
# ─────────────────────────────────────────────────────────────────────────────

async def calculate_receptor_exposure(
    pool,
    spill_id: str,
    horizon_hours: float,
    footprint_wkt: str,
    receptor_type: str,
) -> Dict:
    """
    Calculate ecological exposure for one receptor type at one forecast horizon.
    
    Args:
        pool: asyncpg connection pool
        spill_id: UUID of the spill
        horizon_hours: Forecast horizon (e.g., 1, 3, 6, 12, 24)
        footprint_wkt: WKT of forecast geometry (EPSG:4326)
        receptor_type: 'mangrove' | 'coral_reef' | 'mpa' | 'sensitive_coastline'
    
    Returns:
        Dict with:
            overlap_area_km2: Intersection area in km²
            receptor_area_km2: Total receptor area in km²
            spill_area_km2: Spill footprint area in km²
            exposure_pct: (overlap / receptor) * 100
            spill_share_pct: (overlap / spill) * 100
            category: None|Low|Medium|High|Critical
            sensitivity_tier: from receptor (not weighted)
            protection_status: from receptor
            affected_receptor_count: Number of receptor geometries hit
    """
    # Query aggregated areas using PostGIS
    # - ST_Intersects for topology (uses GIST index)
    # - ST_Intersection to get overlap geometry
    # - ST_Area::geography for metric area in m²
    
    row = await pool.fetchrow(
        """
        WITH forecast_geom AS (
            SELECT ST_SetSRID(ST_GeomFromText($1), 4326) AS geom
        ),
        receptor_hits AS (
            SELECT 
                r.id,
                r.sensitivity_tier,
                r.protection_status,
                ST_Area(r.geom::geography) AS receptor_area_m2,
                ST_Area(
                    ST_Intersection(r.geom, f.geom)::geography
                ) AS overlap_area_m2
            FROM ecological_receptors r, forecast_geom f
            WHERE r.receptor_type = $2
              AND ST_Intersects(r.geom, f.geom)
        )
        SELECT
            COALESCE(SUM(overlap_area_m2), 0) AS total_overlap_m2,
            COALESCE(
                SUM(receptor_area_m2) FILTER (WHERE overlap_area_m2 > 0),
                (SELECT SUM(ST_Area(geom::geography)) 
                 FROM ecological_receptors 
                 WHERE receptor_type = $2)
            ) AS total_receptor_m2,
            COUNT(*) AS affected_count,
            MODE() WITHIN GROUP (ORDER BY sensitivity_tier) AS mode_sensitivity,
            MODE() WITHIN GROUP (ORDER BY protection_status) AS mode_protection
        FROM receptor_hits
        """,
        footprint_wkt, receptor_type
    )
    
    # Get spill area
    spill_area_m2_row = await pool.fetchval(
        "SELECT ST_Area(ST_SetSRID(ST_GeomFromText($1), 4326)::geography)",
        footprint_wkt
    )
    spill_area_m2 = float(spill_area_m2_row or 0)
    
    # Extract values
    total_overlap_m2 = float(row["total_overlap_m2"] or 0)
    total_receptor_m2 = float(row["total_receptor_m2"] or 1)  # avoid div/0
    affected_count = int(row["affected_count"] or 0)
    sensitivity_tier = row["mode_sensitivity"]
    protection_status = row["mode_protection"]
    
    # Convert to km²
    overlap_km2 = total_overlap_m2 / 1_000_000
    receptor_km2 = total_receptor_m2 / 1_000_000
    spill_km2 = spill_area_m2 / 1_000_000
    
    # Calculate percentages
    exposure_pct = (total_overlap_m2 / total_receptor_m2 * 100) if total_receptor_m2 > 0 else 0
    spill_share_pct = (total_overlap_m2 / spill_area_m2 * 100) if spill_area_m2 > 0 else 0
    
    # Classify
    category = classify_exposure(exposure_pct)
    
    return {
        "overlap_area_km2": round(overlap_km2, 4),
        "receptor_area_km2": round(receptor_km2, 4),
        "spill_area_km2": round(spill_km2, 4),
        "exposure_pct": round(exposure_pct, 4),
        "spill_share_pct": round(spill_share_pct, 4),
        "category": category,
        "sensitivity_tier": sensitivity_tier,
        "protection_status": protection_status,
        "affected_receptor_count": affected_count,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TIME TO FIRST EXPOSURE
# ─────────────────────────────────────────────────────────────────────────────

def calculate_time_to_first_exposure(
    impact_results: List[Dict],
    receptor_type: str,
) -> Optional[float]:
    """
    Find the earliest horizon where exposure_pct > 0 for a receptor type.
    
    Args:
        impact_results: List of exposure results sorted by horizon_hours
        receptor_type: Receptor type to check
    
    Returns:
        horizon_hours of first exposure, or None if no exposure at any horizon
    """
    for result in impact_results:
        if result["receptor_type"] == receptor_type and result["exposure_pct"] > 0:
            return result["horizon_hours"]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-HORIZON CALCULATION
# ─────────────────────────────────────────────────────────────────────────────

async def calculate_ecological_impact_for_spill(
    pool,
    spill_id: str,
) -> List[Dict]:
    """
    Calculate ecological impact for all horizons and receptor types for a spill.
    
    Args:
        pool: asyncpg connection pool
        spill_id: UUID of the spill
    
    Returns:
        List of impact results, one per (horizon, footprint_type, receptor_type)
    """
    # Fetch all forecast geometries for this spill
    forecasts = await pool.fetch(
        """
        SELECT horizon_hours,
               ST_AsText(geom) AS geom_wkt,
               ST_AsText(probability_90_geom) AS prob90_wkt
        FROM forecasts
        WHERE spill_id = $1
        ORDER BY horizon_hours ASC
        """,
        spill_id
    )
    
    if not forecasts:
        return []
    
    receptor_types = ["mangrove", "coral_reef", "mpa", "sensitive_coastline"]
    results = []
    
    for fc in forecasts:
        horizon_hours = float(fc["horizon_hours"])
        geom_wkt = fc["geom_wkt"]
        prob90_wkt = fc["prob90_wkt"]
        
        # Calculate for best_estimate (always available)
        for receptor_type in receptor_types:
            exposure = await calculate_receptor_exposure(
                pool, spill_id, horizon_hours, geom_wkt, receptor_type
            )
            
            results.append({
                "spill_id": spill_id,
                "horizon_hours": horizon_hours,
                "footprint_type": "best_estimate",
                "receptor_type": receptor_type,
                **exposure,
                "metadata": {}
            })
        
        # Calculate for probability_90 if available
        if prob90_wkt:
            for receptor_type in receptor_types:
                exposure = await calculate_receptor_exposure(
                    pool, spill_id, horizon_hours, prob90_wkt, receptor_type
                )
                
                results.append({
                    "spill_id": spill_id,
                    "horizon_hours": horizon_hours,
                    "footprint_type": "probability_90",
                    "receptor_type": receptor_type,
                    **exposure,
                    "metadata": {}
                })
        else:
            # Fallback: use best_estimate for probability_90
            for receptor_type in receptor_types:
                exposure = await calculate_receptor_exposure(
                    pool, spill_id, horizon_hours, geom_wkt, receptor_type
                )
                
                results.append({
                    "spill_id": spill_id,
                    "horizon_hours": horizon_hours,
                    "footprint_type": "probability_90",
                    "receptor_type": receptor_type,
                    **exposure,
                    "metadata": {"fallback_to_best_estimate": True}
                })
    
    # Calculate time_to_first_exposure for each receptor type
    for receptor_type in receptor_types:
        first_exposure_hours = calculate_time_to_first_exposure(
            [r for r in results if r["footprint_type"] == "best_estimate"],
            receptor_type
        )
        
        # Update all results for this receptor with time_to_first_exposure
        for result in results:
            if result["receptor_type"] == receptor_type:
                result["time_to_first_exposure_hours"] = first_exposure_hours
    
    return results
