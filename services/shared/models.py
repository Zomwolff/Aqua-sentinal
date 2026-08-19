"""Shared Pydantic v2 data models for the Aqua-Sentinel AIS pipeline."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ──────────────────────────────────────────────────────────────────────────────
# AIS Raw / Clean
# ──────────────────────────────────────────────────────────────────────────────

class AISRecord(BaseModel):
    """Raw AIS message as received from any provider."""
    mmsi: int
    lat: float
    lon: float
    speed_knots: Optional[float] = None
    course: Optional[float] = None
    heading: Optional[float] = None
    nav_status: Optional[int] = None
    timestamp: datetime
    vessel_name: Optional[str] = None
    vessel_type: Optional[int] = None  # AIS ship type code 0-99
    vessel_type_str: Optional[str] = None  # human-readable type
    imo_number: Optional[int] = None
    call_sign: Optional[str] = None
    flag: Optional[str] = None  # 2-char country code
    draught: Optional[float] = None
    destination: Optional[str] = None
    eta: Optional[datetime] = None
    cargo_type: Optional[str] = None
    length_m: Optional[float] = None
    width_m: Optional[float] = None
    raw_source: str = "aisstream"


class CleanAISRecord(AISRecord):
    """Validated and normalised AIS record ready for downstream processing."""
    quality_flag: str = "raw"  # raw | interpolated | corrected
    ingested_at: datetime = Field(default_factory=datetime.utcnow)


# ──────────────────────────────────────────────────────────────────────────────
# Analytics
# ──────────────────────────────────────────────────────────────────────────────

class ProximityEvent(BaseModel):
    mmsi: int
    distance_m: float
    timestamp: datetime


class VesselFeatures(BaseModel):
    mmsi: int
    window_start: datetime
    window_end: datetime
    ping_count: int = 0
    avg_speed: Optional[float] = None
    speed_variance: Optional[float] = None
    max_speed: Optional[float] = None
    course_variance: Optional[float] = None
    heading_change_rate: Optional[float] = None
    loitering_score: Optional[float] = None
    distance_traveled_km: Optional[float] = None
    proximity_events: List[ProximityEvent] = Field(default_factory=list)
    quality: str = "ok"  # ok | low | insufficient


# ──────────────────────────────────────────────────────────────────────────────
# Anomaly Detection
# ──────────────────────────────────────────────────────────────────────────────

class AnomalyEvent(BaseModel):
    mmsi: int
    window_start: datetime
    anomaly_type: str  # sudden_stop | erratic_course | speed_anomaly | loitering_anomaly | route_deviation | ais_gap
    severity: str  # LOW | MEDIUM | HIGH
    evidence: Dict[str, Any] = Field(default_factory=dict)
    source: str = "rules"  # rules | statistical | isolation_forest


# ──────────────────────────────────────────────────────────────────────────────
# Trust / Spoof
# ──────────────────────────────────────────────────────────────────────────────

class TrustScore(BaseModel):
    mmsi: int
    timestamp: datetime
    discrepancy_distance_m: Optional[float] = None
    instant_trust_score: float  # 0-1
    rolling_trust_score: float  # 0-1, EMA
    speed_jump_flag: bool = False
    identity_change_flag: bool = False
    mmsi_validity_flag: bool = True
    flag: Optional[str] = None  # None | spoofing_suspected


# ──────────────────────────────────────────────────────────────────────────────
# STS Events
# ──────────────────────────────────────────────────────────────────────────────

class STSEvent(BaseModel):
    vessel_a: int
    vessel_b: int
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_minutes: Optional[float] = None
    avg_distance_m: Optional[float] = None
    min_distance_m: Optional[float] = None
    avg_combined_speed_knots: Optional[float] = None
    confidence: float = 0.0
    lat: Optional[float] = None  # centroid of encounter
    lon: Optional[float] = None


# ──────────────────────────────────────────────────────────────────────────────
# Risk Engine
# ──────────────────────────────────────────────────────────────────────────────

class ContributingFactor(BaseModel):
    factor: str
    weight: float
    value: float
    contribution: float


class VesselRiskScore(BaseModel):
    mmsi: int
    risk_score: float  # 0-100
    tier: str  # LOW | MEDIUM | HIGH | CRITICAL
    contributing_factors: List[ContributingFactor] = Field(default_factory=list)
    recommended_action: str
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ──────────────────────────────────────────────────────────────────────────────
# Ingestion
# ──────────────────────────────────────────────────────────────────────────────

class IngestResult(BaseModel):
    accepted: int = 0
    rejected: int = 0
    rejection_reasons: Dict[str, int] = Field(default_factory=dict)

    def add_rejection(self, reason: str) -> None:
        self.rejected += 1
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1
