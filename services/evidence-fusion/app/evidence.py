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
DEFAULT_SPATIAL_WINDOW_M = 20_000.0
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


def _as_aware_datetime(value: datetime) -> datetime:
    """Treat naive timestamps as UTC, matching the existing service behavior."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def build_vessel_correlation_sql(limit: Optional[int] = None) -> str:
    """Geography-based vessel-risk correlation query.

    Parameters ($1..$5): $1 start_ts, $2 end_ts (temporal window), $3 longitude,
    $4 latitude, $5 spatial_window_m.

    Uses PostGIS geography ST_DWithin against the candidate centroid — never
    longitude/latitude difference arithmetic (docs/spatial.md).
    """
    centroid_point = make_point_sql("$3", "$4")
    distance_expr = distance_m_sql("vp.geom", centroid_point)
    proximity_expr = dwithin_sql("vp.geom", centroid_point, "$5")
    tiers = ", ".join(f"'{t}'" for t in HIGH_RISK_TIERS)
    sql = f"""
        SELECT vr.vessel_id,
               v.mmsi,
               v.vessel_type,
               vr.risk_score,
               vr.tier::text AS tier,
               vr.recommended_action,
               vp.latitude AS position_lat,
               vp.longitude AS position_lon,
               vp.timestamp AS position_timestamp,
               {distance_expr} AS distance_m,
               MIN({distance_expr}) OVER (PARTITION BY vp.vessel_id) AS closest_approach_m
        FROM vessel_positions vp
        JOIN vessels v ON v.id = vp.vessel_id
        JOIN vessel_risk_scores vr ON vr.vessel_id = v.id
        WHERE vr.tier IN ({tiers})
          AND vp.timestamp BETWEEN $1 AND $2
          AND {proximity_expr}
          AND vp.geom IS NOT NULL
        ORDER BY distance_m ASC, v.mmsi ASC
    """
    if limit is not None:
        sql += f"\nLIMIT {int(limit)}"
    return sql


def select_correlated_vessels(
    records: List[Dict[str, Any]],
    spatial_window_m: float,
    temporal_window_hours: float,
    candidate_acquisition: datetime,
) -> List[Dict[str, Any]]:
    """Return every qualifying vessel candidate in deterministic order."""
    acq = _as_aware_datetime(candidate_acquisition)
    half = timedelta(hours=float(temporal_window_hours))

    qualifying_by_vessel: Dict[Any, Dict[str, Any]] = {}
    for record in records:
        distance = record.get("distance_m")
        if distance is None or not (0.0 <= float(distance) <= float(spatial_window_m)):
            continue
        ts = record.get("position_timestamp")
        if ts is None:
            continue
        ts_dt = _as_aware_datetime(ts)
        if not (acq - half <= ts_dt <= acq + half):
            continue
        time_gap_hours = abs((acq - ts_dt).total_seconds()) / 3600.0
        vessel_id = record.get("vessel_id", record.get("mmsi"))
        record_approach = record.get("closest_approach_m")
        record_approach = float(record_approach) if record_approach is not None else float(record["distance_m"])
        existing = qualifying_by_vessel.get(vessel_id)
        if existing is None or (
            float(record["distance_m"]), str(record.get("mmsi") or "")
        ) < (
            float(existing["distance_m"]), str(existing.get("mmsi") or "")
        ):
            selected = dict(record)
            selected["time_gap_hours"] = time_gap_hours
            selected["closest_approach_m"] = min(
                record_approach,
                float(existing.get("closest_approach_m", record["distance_m"]))
                if existing is not None
                else record_approach,
            )
            qualifying_by_vessel[vessel_id] = selected
        elif existing is not None:
            existing["closest_approach_m"] = min(
                float(existing.get("closest_approach_m", existing["distance_m"])),
                record_approach,
            )

    return sorted(
        qualifying_by_vessel.values(),
        key=lambda r: (float(r["distance_m"]), str(r.get("mmsi") or "")),
    )


def select_correlated_vessel(
    records: List[Dict[str, Any]],
    spatial_window_m: float,
    temporal_window_hours: float,
    candidate_acquisition: datetime,
) -> Optional[Dict[str, Any]]:
    """Backward-compatible helper returning the nearest qualifying vessel."""
    qualifying = select_correlated_vessels(
        records,
        spatial_window_m=spatial_window_m,
        temporal_window_hours=temporal_window_hours,
        candidate_acquisition=candidate_acquisition,
    )
    return qualifying[0] if qualifying else None


def _normalise_vessel_for_event(record: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a correlated vessel record for the incident.fused payload."""
    vessel = {
        "vessel_id": int(record["vessel_id"]),
        "mmsi": str(record["mmsi"]),
        "vessel_type": record.get("vessel_type"),
        "position_lat": float(record.get("position_lat") if record.get("position_lat") is not None else record.get("latitude", 0.0)),
        "position_lon": float(record.get("position_lon") if record.get("position_lon") is not None else record.get("longitude", 0.0)),
        "position_timestamp": (
            record["position_timestamp"].isoformat()
            if record.get("position_timestamp") is not None and hasattr(record["position_timestamp"], "isoformat")
            else record.get("position_timestamp")
        ),
        "distance_m": float(record["distance_m"]),
        "closest_approach_m": (
            float(record["closest_approach_m"])
            if record.get("closest_approach_m") is not None
            else None
        ),
        "time_gap_hours": (
            float(record["time_gap_hours"])
            if record.get("time_gap_hours") is not None
            else None
        ),
        "high_anomaly_count": int(record.get("high_anomaly_count", 0)),
        "medium_anomaly_count": int(record.get("medium_anomaly_count", 0)),
    }
    for key in ("risk_score", "tier", "recommended_action"):
        if key in record and record.get(key) is not None:
            vessel[key] = record[key]
    return vessel


def fuse_evidence(
    candidate: Dict[str, Any],
    correlated_vessels: Optional[List[Dict[str, Any]]] | Optional[Dict[str, Any]],
    *,
    centroid_lat: Optional[float] = None,
    centroid_lon: Optional[float] = None,
    area_m2: Optional[float] = None,
    geom_geojson: Optional[str] = None,
    environment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the ``incident.fused`` payload.

    ``candidate`` must contain candidate_id, scene_id, confidence,
    classification_label; scene metadata (orbit/polarization/resolution) is
    optional and forwarded when present. The candidate's real geometry is
    forwarded when the DB row was found (centroid_lat/centroid_lon/area_m2/
    geom_geojson), so downstream consumers never have to guess coordinates.

    The result NEVER contains attribution fields.
    """
    if isinstance(correlated_vessels, dict):
        correlated_vessels = [correlated_vessels]
    elif correlated_vessels is None:
        correlated_vessels = []

    event: Dict[str, Any] = {
        "candidate_id": candidate["candidate_id"],
        "scene_id": candidate["scene_id"],
        "confidence": float(candidate["confidence"]),
        "classification_label": candidate["classification_label"],
        "acquisition_time": candidate.get("acquisition_time"),
        "correlated_vessel_id": None,
        "correlated_vessel": None,
        "candidates": [],
        "environment": environment or {
            "wind_speed_ms": 0.0,
            "wind_dir_deg": 0.0,
            "current_speed_ms": 0.0,
            "current_dir_deg": 0.0,
            "timestamp": None,
            "source": None,
        },
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

    event["candidates"] = [
        _normalise_vessel_for_event(record)
        for record in correlated_vessels
        if record is not None
    ]
    if event["candidates"]:
        event["correlated_vessel_id"] = int(event["candidates"][0]["vessel_id"])
        event["correlated_vessel"] = event["candidates"][0]
    return event