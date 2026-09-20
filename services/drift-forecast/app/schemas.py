"""
Pydantic schemas for the Origin Inference API.

Request/response models only — no science here.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# ── GeoJSON input ──────────────────────────────────────────────────────────────

class PolygonGeometry(BaseModel):
    type: Literal["Polygon"]
    coordinates: List[List[List[float]]] = Field(..., min_length=1)

    @field_validator("coordinates")
    @classmethod
    def validate_polygon_coords(cls, v: List[List[List[float]]]) -> List[List[List[float]]]:
        for ring in v:
            if len(ring) < 4:
                raise ValueError("Each ring must have at least 4 coordinate pairs (closed polygon)")
            for pt in ring:
                if len(pt) < 2:
                    raise ValueError("Each coordinate must have at least [lon, lat]")
                lon, lat = pt[0], pt[1]
                if not (-180.0 <= lon <= 180.0):
                    raise ValueError(f"Longitude {lon} out of range [-180, 180]")
                if not (-90.0 <= lat <= 90.0):
                    raise ValueError(f"Latitude {lat} out of range [-90, 90]")
        return v


class MultiPolygonGeometry(BaseModel):
    type: Literal["MultiPolygon"]
    coordinates: List[List[List[List[float]]]] = Field(..., min_length=1)

    @field_validator("coordinates")
    @classmethod
    def validate_multipolygon_coords(
        cls, v: List[List[List[List[float]]]]
    ) -> List[List[List[List[float]]]]:
        for polygon in v:
            for ring in polygon:
                if len(ring) < 4:
                    raise ValueError("Each ring must have at least 4 coordinate pairs")
                for pt in ring:
                    if len(pt) < 2:
                        raise ValueError("Each coordinate must have at least [lon, lat]")
                    lon, lat = pt[0], pt[1]
                    if not (-180.0 <= lon <= 180.0):
                        raise ValueError(f"Longitude {lon} out of range [-180, 180]")
                    if not (-90.0 <= lat <= 90.0):
                        raise ValueError(f"Latitude {lat} out of range [-90, 90]")
        return v


FootprintGeometry = Union[PolygonGeometry, MultiPolygonGeometry]


# ── Geometry helpers ───────────────────────────────────────────────────────────

def _geom_centroid_and_area(geom: FootprintGeometry) -> Tuple[Tuple[float, float], float]:
    """
    Derive (centroid_lat, centroid_lon) and area_m2 from a GeoJSON geometry.
    Uses shapely.  Centroid coordinates are WGS-84 degrees.
    Area uses flat-Earth approximation (accurate to ~1% at lat < 45 deg).
    """
    from shapely.geometry import shape  # type: ignore[import]

    geo_dict = geom.model_dump()
    shp = shape(geo_dict)

    c = shp.centroid
    centroid_lon, centroid_lat = c.x, c.y

    lat_rad = math.radians(centroid_lat)
    m_per_deg_lat = 111_319.0
    m_per_deg_lon = 111_319.0 * math.cos(lat_rad)
    area_m2 = abs(shp.area) * m_per_deg_lat * m_per_deg_lon

    return (centroid_lat, centroid_lon), area_m2


# ── Request ────────────────────────────────────────────────────────────────────

class InferOriginRequest(BaseModel):
    """
    POST /api/v1/origin/infer request body.

    Only observational inputs are accepted.
    Scientific parameters (backward_horizon_hours=96, n_candidates=100) are
    frozen server-side and cannot be overridden by the client.
    """

    model_config = {"extra": "forbid"}

    detection_time: datetime = Field(
        ...,
        description="ISO-8601 UTC detection timestamp from SAR acquisition",
        examples=["2020-08-10T01:37:55Z"],
    )
    observed_footprint: FootprintGeometry = Field(
        ...,
        description="Observed spill footprint as GeoJSON Polygon or MultiPolygon",
    )

    @field_validator("detection_time")
    @classmethod
    def ensure_utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError(
                "detection_time must be timezone-aware (append Z or +00:00)"
            )
        return v.astimezone(timezone.utc)


# ── Job / status response ──────────────────────────────────────────────────────

class JobAcceptedResponse(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"
    message: str = (
        "Origin inference job queued. "
        "Poll GET /api/v1/origin/jobs/{job_id} for status."
    )


class ErrorDetail(BaseModel):
    code: str
    message: str


# ── Result sub-schemas (mirrors OriginProbabilityResult fields) ────────────────

class PosteriorCandidateOut(BaseModel):
    candidate_id: int
    source_lat: float
    source_lon: float
    release_time: str   # ISO-8601
    posterior_weight: float


class SpatialCredibleRegionOut(BaseModel):
    level: float
    cumulative_weight: float
    n_candidates: int
    spatial_extent_km: float
    candidates: List[PosteriorCandidateOut]
    hull_wkt: Optional[str] = None


class SpatialPosteriorOut(BaseModel):
    map_lat: float
    map_lon: float
    weighted_centroid_lat: float
    weighted_centroid_lon: float
    weighted_std_lat_m: float
    weighted_std_lon_m: float
    weighted_rms_spread_m: float
    credible_50: SpatialCredibleRegionOut
    credible_90: SpatialCredibleRegionOut


class TemporalPosteriorOut(BaseModel):
    map_release_time: str
    weighted_mean_release_time: str
    credible_50_lo: str
    credible_50_hi: str
    credible_90_lo: str
    credible_90_hi: str
    spread_hours: float
    distribution: List[Tuple[str, float]]


class PosteriorDiagnosticsOut(BaseModel):
    ess: float
    ess_ratio: float
    max_posterior_weight: float
    n_total_candidates: int
    n_nonzero_weight_candidates: int
    n_significant_candidates: int
    n_unique_release_times: int
    candidate_spatial_extent_km: float
    candidate_time_extent_h: float
    quality_flags: List[str]
    warnings: List[str]


class ProvenanceOut(BaseModel):
    detection_time: str
    generation_method: str
    inference_status: str
    n_forcing_samples: Optional[int] = None
    model_info: Dict[str, Any] = Field(default_factory=dict)


class OriginResultOut(BaseModel):
    origin: Dict[str, Any]          # map_lat, map_lon, map_release_time
    spatial_posterior: SpatialPosteriorOut
    temporal_posterior: TemporalPosteriorOut
    diagnostics: PosteriorDiagnosticsOut
    provenance: ProvenanceOut


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    result: Optional[OriginResultOut] = None
    error: Optional[ErrorDetail] = None