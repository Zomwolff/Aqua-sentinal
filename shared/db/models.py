from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Vessel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    imo_number: Optional[str] = None
    mmsi: str
    name: Optional[str] = None
    vessel_type: Optional[str] = None
    flag: Optional[str] = None
    length_m: Optional[Decimal] = None
    width_m: Optional[Decimal] = None
    gross_tonnage: Optional[Decimal] = None
    operator: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class VesselPosition(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vessel_id: int
    timestamp: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    geom: Optional[str] = None
    speed_knots: Optional[Decimal] = None
    course_deg: Optional[Decimal] = None
    heading_deg: Optional[Decimal] = None
    nav_status: Optional[str] = None
    source: Optional[str] = None


class SpillIncident(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    detected_at: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    geom: Optional[str] = None
    centroid: str
    area_km2: Optional[Decimal] = None
    confidence: Optional[Decimal] = None
    source: Optional[str] = None
    source_image_id: Optional[str] = None
    status: Optional[str] = None


class AttributionResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    spill_id: UUID
    vessel_id: int
    distance_score: Optional[Decimal] = None
    trajectory_score: Optional[Decimal] = None
    wind_score: Optional[Decimal] = None
    time_score: Optional[Decimal] = None
    behavior_score: Optional[Decimal] = None
    final_score: Decimal
    model_version: Optional[str] = None
    computed_at: Optional[datetime] = None


class Forecast(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    spill_id: UUID
    forecast_time: Optional[datetime] = None
    generated_at: Optional[datetime] = None
    horizon_hours: Optional[Decimal] = None
    geom: str
    model_version: Optional[str] = None
    confidence: Optional[Decimal] = None


class Severity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    spill_id: UUID
    severity_level: Optional[str] = None
    score: Decimal
    environmental_risk: Optional[Decimal] = None
    population_risk: Optional[Decimal] = None
    economic_risk: Optional[Decimal] = None
    protected_area_risk: Optional[Decimal] = None
    computed_at: Optional[datetime] = None


class ResponseRecommendation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    spill_id: UUID
    recommendation: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    generated_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None


class ProtectedArea(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: Optional[str] = None
    area_type: Optional[str] = None
    geom: str
    metadata: Optional[dict] = None


class EnvironmentalCondition(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    timestamp: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    geom: Optional[str] = None
    wind_speed_kmh: Optional[Decimal] = None
    wind_direction_deg: Optional[Decimal] = None
    current_speed_ms: Optional[Decimal] = None
    current_direction_deg: Optional[Decimal] = None
    source: Optional[str] = None
