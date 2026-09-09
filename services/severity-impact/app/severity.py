"""
Severity Impact V1 — Rule-based consequence assessment.

Implements the finalized Severity-Impact V1 methodology:
- Dominant consequence / maximum receptor impact (no weighted formula)
- Ecological categories inherited from Ecological Impact V1
- Socioeconomic based on port/fishing zone proximity only (no population data)
- Rule-based escalation (breadth, cross-domain, TTFE urgency)
- Severity score [0-100] with fixed category bands (secondary to severity_level)
- Horizon-aware: current, 1h, 3h, 6h, 12h, 24h
- Footprint-aware: best_estimate, probability_90

Pure module — no DB/Redis.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Tuple

# Severity ordering (for comparisons)
SEVERITY_ORDER = {
    "None": 0,
    "Low": 1,
    "Medium": 2,
    "High": 3,
    "Critical": 4,
}

# Map common level names to standard names
SEVERITY_LEVEL_MAP = {
    "LOW": "Low",
    "MODERATE": "Medium",
    "HIGH": "High",
    "CRITICAL": "Critical",
    "None": "None",
    "Low": "Low",
    "Medium": "Medium",
    "High": "High",
    "Critical": "Critical",
}

# Fixed non-overlapping score bands
# External representation: LOW=0-24, MODERATE=25-49, HIGH=50-74, CRITICAL=75-100
# Internal None maps to LOW with score=0
SCORE_BANDS = {
    "None": (0, 0),       # Internal None → external LOW with score=0
    "Low": (0, 24),       # External LOW: 0-24
    "Medium": (25, 49),   # External MODERATE: 25-49
    "High": (50, 74),     # External HIGH: 50-74
    "Critical": (75, 100),  # External CRITICAL: 75-100
}


def normalize_severity(severity: str) -> str:
    """Normalize severity level to standard names (Low/Medium/High/Critical/None)."""
    return SEVERITY_LEVEL_MAP.get(severity, severity)


def to_db_severity(internal_level: str) -> str:
    """
    Convert internal severity level to database/Redis enum format.
    
    Database enum: LOW, MODERATE, HIGH, CRITICAL
    Internal representation: None, Low, Medium, High, Critical
    
    Args:
        internal_level: Internal severity level (None/Low/Medium/High/Critical)
    
    Returns:
        Database enum value (LOW/MODERATE/HIGH/CRITICAL)
    """
    mapping = {
        "None": "LOW",       # None maps to LOW with score=0
        "Low": "LOW",
        "Medium": "MODERATE",  # KEY: Medium → MODERATE
        "High": "HIGH",
        "Critical": "CRITICAL",
    }
    return mapping.get(internal_level, "LOW")


def compare_severity(s1: str, s2: str) -> int:
    """Compare two severity levels. Returns: -1 (s1<s2), 0 (s1==s2), 1 (s1>s2)."""
    s1_norm = normalize_severity(s1)
    s2_norm = normalize_severity(s2)
    o1 = SEVERITY_ORDER.get(s1_norm, -1)
    o2 = SEVERITY_ORDER.get(s2_norm, -1)
    if o1 < o2:
        return -1
    elif o1 > o2:
        return 1
    return 0


def max_severity(severities: List[str]) -> str:
    """Return maximum severity from list (None < Low < Medium < High < Critical)."""
    if not severities:
        return "None"
    max_sev = severities[0]
    for sev in severities[1:]:
        if compare_severity(sev, max_sev) > 0:
            max_sev = sev
    return normalize_severity(max_sev)


def determine_ecological_severity(ecological_impacts: List[Dict]) -> Tuple[str, List[str], int]:
    """
    Determine ecological severity by taking maximum receptor category.
    
    Args:
        ecological_impacts: List of ecological impact records with 'category' and 'receptor_type'
    
    Returns:
        Tuple of (severity, high_critical_receptors, affected_count)
    """
    if not ecological_impacts:
        return "None", [], 0
    
    categories = [imp.get("category", "None") for imp in ecological_impacts]
    max_category = max_severity(categories)
    
    # Track DISTINCT receptor types with High or Critical exposure (for breadth escalation).
    # Uses a set so that multiple records for the same receptor type count as one.
    high_critical_receptor_types: set = set()
    affected_receptors = set()

    for imp in ecological_impacts:
        category = normalize_severity(imp.get("category", "None"))
        receptor = imp.get("receptor_type", "")

        if category in ("High", "Critical"):
            high_critical_receptor_types.add(receptor)

        if category != "None":
            affected_receptors.add(receptor)

    return max_category, list(high_critical_receptor_types), len(affected_receptors)


def determine_socioeconomic_severity(
    ports_within_5km: int,
    fishing_zones_within_10km: int
) -> Tuple[str, List[str]]:
    """
    Determine socioeconomic severity based on port/fishing zone proximity.
    
    V1 uses ONLY available data: port and fishing_zone from protected_areas table.
    NO population density data available.
    
    Rules:
    - Port within 5 km → High
    - Fishing zone within 10 km → Medium
    - Neither → Low
    
    Args:
        ports_within_5km: Count of ports within 5km
        fishing_zones_within_10km: Count of fishing zones within 10km
    
    Returns:
        Tuple of (severity, drivers)
    """
    drivers = []
    
    if ports_within_5km > 0:
        drivers.append(f"port_within_5km_count_{ports_within_5km}")
        return "High", drivers
    
    if fishing_zones_within_10km > 0:
        drivers.append(f"fishing_zone_within_10km_count_{fishing_zones_within_10km}")
        return "Medium", drivers
    
    return "Low", ["no_socioeconomic_receptors_nearby"]


def apply_escalation_rules(
    base_severity: str,
    ecological_severity: str,
    socioeconomic_severity: str,
    high_critical_receptor_count: int,
    ttfe_hours: Optional[float]
) -> Tuple[str, bool, List[str]]:
    """
    Apply rule-based escalation to determine final severity.
    
    Escalation rules (max +1 tier):
    1. Breadth: 2+ ecological receptors with High or Critical
    2. Cross-domain: Ecological High+ AND Socioeconomic High+
    3. TTFE urgency: First exposure < 3 hours (if applicable)
    
    Args:
        base_severity: Initial severity from dominant consequence
        ecological_severity: Ecological domain severity
        socioeconomic_severity: Socioeconomic domain severity
        high_critical_receptor_count: Number of High/Critical ecological receptors
        ttfe_hours: Time-to-first-exposure (hours)
    
    Returns:
        Tuple of (final_severity, escalation_applied, escalation_reasons)
    """
    escalation_reasons = []
    escalation_applied = False
    
    base_order = SEVERITY_ORDER.get(normalize_severity(base_severity), 0)
    escalated_order = base_order
    
    # Rule 1: Breadth escalation (2+ High/Critical receptors)
    if high_critical_receptor_count >= 2:
        escalation_reasons.append(f"multiple_high_ecological_receptors_count_{high_critical_receptor_count}")
        escalated_order = max(escalated_order, base_order + 1)
        escalation_applied = True
    
    # Rule 2: Cross-domain escalation
    eco_order = SEVERITY_ORDER.get(normalize_severity(ecological_severity), 0)
    socio_order = SEVERITY_ORDER.get(normalize_severity(socioeconomic_severity), 0)
    
    if eco_order >= SEVERITY_ORDER["High"] and socio_order >= SEVERITY_ORDER["High"]:
        escalation_reasons.append("cross_domain_escalation_both_high")
        escalated_order = max(escalated_order, base_order + 1)
        escalation_applied = True
    
    # Rule 3: TTFE urgency escalation (<3 hours)
    if ttfe_hours is not None and ttfe_hours < 3.0 and ttfe_hours > 0:
        escalation_reasons.append(f"ttfe_urgency_{ttfe_hours:.1f}h")
        escalated_order = max(escalated_order, base_order + 1)
        escalation_applied = True
    
    # Cap escalation at +1 tier and max at Critical
    escalated_order = min(escalated_order, base_order + 1)
    escalated_order = min(escalated_order, SEVERITY_ORDER["Critical"])
    
    # Convert order back to severity level
    for level, order in SEVERITY_ORDER.items():
        if order == escalated_order:
            return level, escalation_applied, escalation_reasons
    
    return base_severity, False, []


def calculate_severity_score(
    severity_level: str,
    exposure_intensity: float,
    breadth_ratio: float,
    urgency_factor: float
) -> float:
    """
    Calculate 0-100 severity score within fixed category band.
    
    Score is SECONDARY to severity_level and must never override it.
    
    Band ranges:
    - None: 0
    - Low: 1-24
    - Medium: 25-49
    - High: 50-74
    - Critical: 75-100
    
    Within-band intensity components:
    - 50% exposure intensity (e.g., max ecological exposure_pct / 100)
    - 30% breadth (affected receptors / total receptors)
    - 20% urgency (TTFE factor)
    
    Args:
        severity_level: Final severity tier
        exposure_intensity: Exposure intensity [0-1] (e.g., max exposure_pct / 100)
        breadth_ratio: Breadth ratio [0-1] (affected / total receptors)
        urgency_factor: Urgency factor [0-1] (from TTFE)
    
    Returns:
        Score [0-100] within the severity_level's band
    """
    level_norm = normalize_severity(severity_level)
    
    if level_norm == "None":
        return 0.0
    
    band_min, band_max = SCORE_BANDS[level_norm]
    
    # Calculate intensity [0-1]
    intensity = (0.5 * exposure_intensity + 
                 0.3 * breadth_ratio + 
                 0.2 * urgency_factor)
    intensity = min(1.0, max(0.0, intensity))
    
    # Map intensity to band range
    score = band_min + (band_max - band_min) * intensity
    
    return round(score, 2)


def calculate_urgency_factor(ttfe_hours: Optional[float]) -> float:
    """
    Calculate urgency factor [0-1] from TTFE.
    
    - ttfe < 1h → 1.0 (highest urgency)
    - ttfe < 3h → 0.75
    - ttfe < 6h → 0.5
    - ttfe < 12h → 0.25
    - ttfe >= 12h or None → 0.0 (no urgency)
    """
    if ttfe_hours is None or ttfe_hours <= 0:
        return 0.0
    
    if ttfe_hours < 1.0:
        return 1.0
    elif ttfe_hours < 3.0:
        return 0.75
    elif ttfe_hours < 6.0:
        return 0.5
    elif ttfe_hours < 12.0:
        return 0.25
    else:
        return 0.0


def determine_trajectory(current_severity: str, future_severity: str) -> str:
    """
    Determine trajectory by comparing current vs future (24h probability_90) severity.
    
    Returns: "IMPROVING" | "STABLE" | "WORSENING"
    """
    comp = compare_severity(future_severity, current_severity)
    
    if comp > 0:
        return "WORSENING"
    elif comp < 0:
        return "IMPROVING"
    else:
        return "STABLE"
