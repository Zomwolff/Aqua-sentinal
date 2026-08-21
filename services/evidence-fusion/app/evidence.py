"""Evidence fusion core: SAR candidate + nearby vessel-risk context (Step 6).

Pure, deterministic, DB/Redis-free. Worker integrates with PostGIS/Redis.

This step performs evidence FUSION only. ``correlated_vessel_id`` means a
vessel-risk record was geographically and temporally correlated with the SAR
candidate — it does NOT mean the vessel caused the spill, and NO source
attribution / culpability / causal fields are ever produced here.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from shared.spatial.geo import distance_m_sql, dwithin_sql, make_point_sql

# Tunable correlation assumptions — NOT validated scientific constants. Their
# production values must come from validation/operational tuning; the project
# had no existing equivalents, so 5 km / 6 h are the proposed initial values.
DEFAULT_SPATIAL_WINDOW_M = 5000.0
DEFAULT_TEMPORAL_WINDOW_HOURS = 6.0

# Existing risk representation reused verbatim: risk_tier_enum tiers HIGH and
# CRITICAL are how this codebase designates high-risk vessels (vessel-risk-engine
# tasks HIGH/CRITICAL, api-gateway counts them as high-risk vessels).
HIGH_RISK_TIERS = ("HIGH", "CRITICAL")

# Never emitted as a fused field — source attribution belongs to a later stage.
FORBIDDEN_ATTRIBUTION_FIELDS = (
    "caused_by",
    "responsible_vessel",
    "source_vessel",
    "attribution",
)


def correlation_windows() -> Tuple[float, float]:
    """Return (spatial_window_m, temporal_window_hours) from configuration."""
    spatial = float(os.environ.get("EVIDENCE_SPATIAL_WINDOW_M", DEFAULT_SPATIAL_WINDOW_M))
    hours = float(os.environ.get("EVIDENCE_TEMPORAL_WINDOW_HOURS", DEFAULT_TEMPORAL_WINDOW_HOURS))
    if spatial <= 0 or hours <= 0:
        raise ValueError("correlation windows must be positive numbers.")
    return spatial, hours


def candidate_lookup_sql() -> str:
    """SQL returning the candidate centroid (lon, lat), geodesic area, GeoJSON
    geometry and acquisition time."""
    return """
        SELECT ST_X(ST_Centroid(geom)) AS centroid_lon,
               ST_Y(ST_Centroid(geom)) AS centroid_lat,
               area_m2,
               ST_AsGeoJSON(geom) AS geom_geojson,
               acquisition_time
        FROM spill_candidates
        WHERE candidate_id = $1
    """


def build_vessel_correlation_sql(limit: int = 20) -> str:
    """Geography-based vessel-risk correlation query.

    Parameters ($1..$5): $1 start_ts, $2 end_ts (temporal window), $3 longitude,
    $4 latitude, $5 spatial_window_m.

    Uses PostGIS geography ST_DWithin against the candidate centroid — never
    longitude/latitude difference arithmetic (docs/spatial.md).
    """
    centroid_point = make_point_sql("$3", "$4")  # ST_SetSRID(ST_MakePoint(lon, lat), 4326)
    distance_expr = distance_m_sql("vp.geom", centroid_point)  # meters via geography
    proximity_expr = dwithin_sql("vp.geom", centroid_point, "$5")  # meters via geography
    tiers = ", ".join(f"'{t}'" for t in HIGH_RISK_TIERS)
    return f"""
        SELECT vr.vessel_id,
               v.mmsi,
               vr.risk_score,
               vr.tier::text AS tier,
               vr.recommended_action,
               vp.timestamp AS position_timestamp,
               {distance_expr} AS distance_m
        FROM vessel_positions vp
        JOIN vessels v ON v.id = vp.vessel_id
        JOIN vessel_risk_scores vr ON vr.vessel_id = v.id
        WHERE vr.tier IN ({tiers})
          AND vp.timestamp BETWEEN $1 AND $2
          AND {proximity_expr}
          AND vp.geom IS NOT NULL
        ORDER BY distance_m ASC, v.mmsi ASC
        LIMIT {int(limit)}
    """


def select_correlated_vessel(
    records: List[Dict[str, Any]],
    spatial_window_m: float,
    temporal_window_hours: float,
    candidate_acquisition: datetime,
) -> Optional[Dict[str, Any]]:
    """Select the single correlated vessel, or None.

    Rows from ``build_vessel_correlation_sql`` are already geometry-filtered by
    PostGIS; this is the deterministic selection + defensive re-check of the
    spatial/temporal windows. Selection rule: nearest geographic distance, ties
    broken by lowest MMSI (documented, deterministic — no attribution implied).
    """
    acq = candidate_acquisition
    if acq.tzinfo is None:
        acq = acq.replace(tzinfo=timezone.utc)
    half = timedelta(hours=float(temporal_window_hours))

    qualifying: List[Dict[str, Any]] = []
    for record in records:
        distance = record.get("distance_m")
        if distance is None or not (0.0 <= float(distance) <= float(spatial_window_m)):
            continue
        ts = record.get("position_timestamp")
        if ts is None:
            continue
        ts_dt = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        if not (acq - half <= ts_dt <= acq + half):
            continue
        qualifying.append(record)

    if not qualifying:
        return None
    return min(qualifying, key=lambda r: (float(r["distance_m"]), str(r["mmsi"])))


def fuse_evidence(
    candidate: Dict[str, Any],
    correlated_vessel: Optional[Dict[str, Any]],
    *,
    centroid_lat: Optional[float] = None,
    centroid_lon: Optional[float] = None,
    area_m2: Optional[float] = None,
    geom_geojson: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ``incident.fused`` payload.

    ``candidate`` must contain candidate_id, scene_id, confidence,
    classification_label; scene metadata (orbit/polarization/resolution) is
    optional and forwarded when present. The candidate's real geometry is
    forwarded when the DB row was found (centroid_lat/centroid_lon/area_m2/
    geom_geojson), so downstream consumers never have to guess coordinates.

    The result NEVER contains attribution fields.
    """
    event: Dict[str, Any] = {
        "candidate_id": candidate["candidate_id"],
        "scene_id": candidate["scene_id"],
        "confidence": float(candidate["confidence"]),
        "classification_label": candidate["classification_label"],
        "acquisition_time": candidate.get("acquisition_time"),
        "correlated_vessel_id": None,
        "correlated_vessel": None,
        "is_synthetic": candidate.get("is_synthetic", False),
    }
    for key in ("orbit", "polarization", "resolution"):
        if key in candidate and candidate.get(key) is not None:
            event[key] = candidate[key]

    # Real spill-candidate geometry (None only when the DB row was missing).
    if centroid_lat is not None and centroid_lon is not None:
        event["lat"] = float(centroid_lat)
        event["lon"] = float(centroid_lon)
    if area_m2 is not None:
        event["area_km2"] = float(area_m2) / 1_000_000.0
    if geom_geojson is not None:
        event["geom_geojson"] = geom_geojson

    if correlated_vessel is not None:
        event["correlated_vessel_id"] = int(correlated_vessel["vessel_id"])
        vessel = {
            "mmsi": str(correlated_vessel["mmsi"]),
            "risk_score": float(correlated_vessel["risk_score"]),
            "tier": correlated_vessel["tier"],
            "recommended_action": correlated_vessel.get("recommended_action"),
            "distance_m": float(correlated_vessel["distance_m"]),
            "position_timestamp": (
                correlated_vessel["position_timestamp"].isoformat()
                if correlated_vessel.get("position_timestamp") is not None
                else None
            ),
        }
        event["correlated_vessel"] = vessel
    return event