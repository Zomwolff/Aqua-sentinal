"""
Physical oil spreading model based on Fay-style gravity/viscous spreading.

This module implements the physical expansion of oil slick footprint independent
of transport (advection/drift) and uncertainty (diffusion/random walk).

OPTION A FIX (2026-09-05):
The implementation now addresses the time-reference mismatch identified in
OIL_SPREAD_V2_PHYSICAL_SPREADING_AUDIT.md:

PROBLEM:
- Fay model assumes t=0 is instantaneous release
- Operational data provides t=0 as detection time (unknown time after release)
- Result: Early forecasts could predict smaller area than observed initial area

SOLUTION (Option A - Differential Spreading):
1. Estimate "effective age" at detection by inverting Fay equation
2. Compute Fay baseline area at detection time
3. Compute Fay area at detection + forecast horizon
4. Apply differential growth: observed_area + (forecast - baseline)
5. Result: Preserves observed initial area, applies Fay physics for growth

This eliminates non-physical "contraction" while preserving Fay formulation.

IMPORTANT SCIENTIFIC NOTES:
- Physical spreading depends on oil properties (density, viscosity, interfacial tension)
- The formulation used here is based on established oil-spill modeling literature
- Default parameters are generic approximations, NOT incident-specific measurements
- Actual spreading rates vary significantly with oil type and environmental conditions

References:
- Fay, J.A. (1969). "The Spread of Oil Slicks on a Calm Sea"
- NOAA OR&R (2002). "Introduction to Oil Chemistry and Fate"
- ITOPF Technical Information Paper 2: "Fate of Marine Oil Spills"
"""
from __future__ import annotations
import math
import os
from typing import Optional

# ============================================================================
# OIL PROPERTY DEFAULTS (GENERIC APPROXIMATIONS)
# ============================================================================
# These are engineering defaults for a medium crude oil when actual oil
# properties are unavailable. They are NOT universal constants.

DEFAULT_OIL_DENSITY_KG_M3 = 900.0          # kg/m³ (medium crude: 850-950)
DEFAULT_OIL_VISCOSITY_M2_S = 0.00001       # m²/s (kinematic viscosity ~10 cSt)
DEFAULT_INTERFACIAL_TENSION_N_M = 0.03     # N/m (oil-water interface)
DEFAULT_WATER_DENSITY_KG_M3 = 1025.0       # kg/m³ (seawater)

# Configurable from environment (V1: allow overrides but provide safe defaults)
OIL_DENSITY = float(os.environ.get("OIL_DENSITY_KG_M3", DEFAULT_OIL_DENSITY_KG_M3))
OIL_VISCOSITY = float(os.environ.get("OIL_KINEMATIC_VISCOSITY_M2_S", DEFAULT_OIL_VISCOSITY_M2_S))
INTERFACIAL_TENSION = float(os.environ.get("OIL_WATER_INTERFACIAL_TENSION_N_M", DEFAULT_INTERFACIAL_TENSION_N_M))
WATER_DENSITY = float(os.environ.get("WATER_DENSITY_KG_M3", DEFAULT_WATER_DENSITY_KG_M3))

# Physical constants
GRAVITY = 9.81  # m/s² (Earth standard gravity)

# ============================================================================
# FAY SPREADING REGIMES
# ============================================================================
# Oil spreading follows sequential regimes (gravity-inertial → gravity-viscous → surface-tension-viscous)
# For most operational spills at sea beyond the first few minutes, the gravity-viscous regime dominates.


def compute_fay_radius_gravity_viscous(
    volume_m3: float,
    elapsed_seconds: float,
    oil_density: float = OIL_DENSITY,
    oil_viscosity: float = OIL_VISCOSITY,
    water_density: float = WATER_DENSITY,
) -> float:
    """
    Compute oil slick radius using Fay's gravity-viscous spreading regime.
    
    This is the dominant regime for most at-sea spills after the initial seconds.
    
    Formula (from Fay 1969, NOAA OR&R 2002):
        r(t) ≈ k * (g * Δρ / ρ_w)^(1/6) * (V^(1/3) / ν^(1/6)) * t^(1/4)
    
    where:
        g = gravitational acceleration
        Δρ = density difference (water - oil)
        ρ_w = water density
        V = oil volume
        ν = oil kinematic viscosity
        t = time
        k = empirical coefficient ≈ 1.0-1.5 (we use 1.14 from NOAA guidance)
    
    Parameters:
        volume_m3: Oil spill volume in cubic metres
        elapsed_seconds: Time since spill release
        oil_density: Oil density (kg/m³)
        oil_viscosity: Oil kinematic viscosity (m²/s)
        water_density: Water density (kg/m³)
    
    Returns:
        Predicted oil slick radius in metres
    
    LIMITATIONS:
    - Assumes calm conditions (no wind/waves breaking up slick)
    - Assumes continuous circular slick (not fragmented)
    - Neglects weathering (evaporation, emulsification)
    - Valid for gravity-viscous regime (typically t > few minutes, < 24 hours)
    - Coefficients are approximate, real spreading rates vary with conditions
    - Thickness assumption introduces significant volume uncertainty
    """
    if volume_m3 <= 0 or elapsed_seconds <= 0:
        return 0.0
    
    # Density difference (buoyancy driver)
    delta_rho = abs(water_density - oil_density)
    if delta_rho < 1.0:
        delta_rho = 1.0  # Safety: prevent division issues
    
    # Prevent negative/zero viscosity
    if oil_viscosity <= 0:
        oil_viscosity = DEFAULT_OIL_VISCOSITY_M2_S
    
    # Fay gravity-viscous formula
    # r ∝ (g * Δρ / ρ_w)^(1/6) * V^(1/3) * ν^(-1/6) * t^(1/4)
    k_empirical = 1.14  # NOAA OR&R empirical coefficient
    
    term1 = (GRAVITY * delta_rho / water_density) ** (1.0 / 6.0)
    term2 = volume_m3 ** (1.0 / 3.0)
    term3 = oil_viscosity ** (-1.0 / 6.0)
    term4 = elapsed_seconds ** (0.25)
    
    radius_m = k_empirical * term1 * term2 * term3 * term4
    
    return float(radius_m)


def estimate_effective_age_hours(
    observed_area_m2: float,
    volume_m3: float,
    oil_density: float = OIL_DENSITY,
    oil_viscosity: float = OIL_VISCOSITY,
    water_density: float = WATER_DENSITY,
) -> float:
    """
    Estimate the effective age of the spill at detection time.
    
    Inverts the Fay equation to estimate how long the spill has been spreading
    to reach the observed area. This addresses the time-reference mismatch
    between the Fay model (t=0 at release) and operational use (t=0 at detection).
    
    Formula: Given r_observed and volume, solve for t:
        t = [r_observed / (k × terms)]^4
    
    Parameters:
        observed_area_m2: Observed spill area at detection (from SAR)
        volume_m3: Estimated oil volume
        oil_density: Oil density (kg/m³)
        oil_viscosity: Oil kinematic viscosity (m²/s)
        water_density: Water density (kg/m³)
    
    Returns:
        Effective age in hours since spill release
    
    NOTE:
    This assumes the Fay model accurately describes the spreading history,
    which may not be true if environmental conditions changed significantly
    or if the spill experienced non-uniform spreading.
    """
    if observed_area_m2 <= 0 or volume_m3 <= 0:
        return 0.0
    
    # Compute equivalent circular radius from observed area
    r_observed = math.sqrt(observed_area_m2 / math.pi)
    
    # Density difference
    delta_rho = abs(water_density - oil_density)
    if delta_rho < 1.0:
        delta_rho = 1.0
    
    # Prevent negative/zero viscosity
    if oil_viscosity <= 0:
        oil_viscosity = DEFAULT_OIL_VISCOSITY_M2_S
    
    # Fay formula terms (excluding time)
    k_empirical = 1.14
    term1 = (GRAVITY * delta_rho / water_density) ** (1.0 / 6.0)
    term2 = volume_m3 ** (1.0 / 3.0)
    term3 = oil_viscosity ** (-1.0 / 6.0)
    
    # Solve for t: r = k × term1 × term2 × term3 × t^(1/4)
    # Therefore: t = [r / (k × term1 × term2 × term3)]^4
    fay_coefficient = k_empirical * term1 * term2 * term3
    
    if fay_coefficient <= 0:
        return 0.0
    
    t_effective_seconds = (r_observed / fay_coefficient) ** 4.0
    t_effective_hours = t_effective_seconds / 3600.0
    
    return float(t_effective_hours)


def estimate_volume_from_area_and_thickness(
    area_m2: float,
    thickness_m: float = 0.0001,  # Default: ~0.1 mm (thin sheen)
) -> float:
    """
    Estimate oil volume from initial observed area and assumed thickness.
    
    IMPORTANT:
    - SAR cannot directly measure oil thickness
    - Thickness assumptions introduce significant uncertainty
    - Typical range: 0.01 mm (sheen) to 10 mm (thick slick)
    - Default 0.1 mm is a conservative mid-range assumption
    
    Parameters:
        area_m2: Initial spill area from SAR detection
        thickness_m: Assumed mean oil thickness
    
    Returns:
        Estimated volume in cubic metres
    """
    return area_m2 * thickness_m


def compute_physical_spread_radius(
    initial_area_m2: float,
    elapsed_hours: float,
    oil_density: float = OIL_DENSITY,
    oil_viscosity: float = OIL_VISCOSITY,
    water_density: float = WATER_DENSITY,
    initial_thickness_m: float = 0.0001,
) -> float:
    """
    Compute the physical oil spreading radius at time t using Option A fix.
    
    OPTION A FIX (from OIL_SPREAD_V2_PHYSICAL_SPREADING_AUDIT.md):
    - Preserves the observed initial area as the baseline at detection time
    - Applies Fay spreading DIFFERENTIALLY from that initial condition
    - Eliminates non-physical "contraction" at early forecast times
    
    Algorithm:
    1. Estimate volume from initial area + assumed thickness
    2. Estimate "effective age" at detection (backward Fay calculation)
    3. Compute Fay area at detection time (should ≈ initial_area_m2)
    4. Compute Fay area at detection + elapsed time
    5. Apply differential spreading: initial_area + growth
    
    Parameters:
        initial_area_m2: Initial spill area (from SAR detection)
        elapsed_hours: Time since spill detection
        oil_density: Oil density (kg/m³)
        oil_viscosity: Oil kinematic viscosity (m²/s)
        water_density: Water density (kg/m³)
        initial_thickness_m: Assumed initial oil thickness (m)
    
    Returns:
        Physical spread radius in metres
    
    NOTES:
    - This is independent of transport (drift/advection)
    - This is independent of uncertainty (diffusion/random walk)
    - Thickness assumption dominates uncertainty in volume estimate
    - Real spreading rates vary with oil type and environmental conditions
    """
    if elapsed_hours <= 0:
        # At t=0, return initial equivalent radius (preserves observed area)
        return math.sqrt(initial_area_m2 / math.pi)
    
    # Estimate volume from observed area and assumed thickness
    volume_m3 = estimate_volume_from_area_and_thickness(initial_area_m2, initial_thickness_m)
    
    # Estimate effective age at detection (how long spill was spreading before detection)
    t_effective_hours = estimate_effective_age_hours(
        observed_area_m2=initial_area_m2,
        volume_m3=volume_m3,
        oil_density=oil_density,
        oil_viscosity=oil_viscosity,
        water_density=water_density,
    )
    
    # Compute Fay area at detection time (reference baseline)
    t_detection_seconds = t_effective_hours * 3600.0
    r_at_detection = compute_fay_radius_gravity_viscous(
        volume_m3=volume_m3,
        elapsed_seconds=t_detection_seconds,
        oil_density=oil_density,
        oil_viscosity=oil_viscosity,
        water_density=water_density,
    )
    area_at_detection = math.pi * r_at_detection ** 2
    
    # Compute Fay area at detection + elapsed time
    t_forecast_seconds = (t_effective_hours + elapsed_hours) * 3600.0
    r_at_forecast = compute_fay_radius_gravity_viscous(
        volume_m3=volume_m3,
        elapsed_seconds=t_forecast_seconds,
        oil_density=oil_density,
        oil_viscosity=oil_viscosity,
        water_density=water_density,
    )
    area_at_forecast = math.pi * r_at_forecast ** 2
    
    # Apply differential spreading from observed initial area
    area_growth = area_at_forecast - area_at_detection
    corrected_area = initial_area_m2 + area_growth
    
    # Ensure area doesn't go negative (shouldn't happen, but safety check)
    corrected_area = max(corrected_area, initial_area_m2)
    
    # Return equivalent circular radius
    corrected_radius = math.sqrt(corrected_area / math.pi)
    
    return corrected_radius


def compute_physical_area(
    initial_area_m2: float,
    elapsed_hours: float,
    oil_density: float = OIL_DENSITY,
    oil_viscosity: float = OIL_VISCOSITY,
    water_density: float = WATER_DENSITY,
    initial_thickness_m: float = 0.0001,
) -> float:
    """
    Compute the physical oil slick area at time t.
    
    Returns:
        Physical area in square metres
    """
    radius = compute_physical_spread_radius(
        initial_area_m2=initial_area_m2,
        elapsed_hours=elapsed_hours,
        oil_density=oil_density,
        oil_viscosity=oil_viscosity,
        water_density=water_density,
        initial_thickness_m=initial_thickness_m,
    )
    return math.pi * radius ** 2


def compute_expansion_ratio(
    initial_area_m2: float,
    forecast_area_m2: float,
) -> float:
    """
    Compute area expansion ratio: A(t) / A(0)
    
    Returns:
        Dimensionless expansion ratio (≥ 1.0)
    """
    if initial_area_m2 <= 0:
        return 1.0
    return forecast_area_m2 / initial_area_m2


def compute_spread_rate_m2_per_hour(
    initial_area_m2: float,
    forecast_area_m2: float,
    elapsed_hours: float,
) -> float:
    """
    Compute average area growth rate: dA/dt
    
    Returns:
        Area growth rate in m²/hour
    """
    if elapsed_hours <= 0:
        return 0.0
    return (forecast_area_m2 - initial_area_m2) / elapsed_hours


# ============================================================================
# V1 INTERFACE WITH OPTIONAL OIL PROPERTIES
# ============================================================================

class OilProperties:
    """
    Container for oil-specific physical properties.
    
    If not provided, defaults to generic medium crude approximations.
    """
    def __init__(
        self,
        density_kg_m3: Optional[float] = None,
        kinematic_viscosity_m2_s: Optional[float] = None,
        interfacial_tension_n_m: Optional[float] = None,
        initial_thickness_m: Optional[float] = None,
    ):
        self.density = density_kg_m3 or OIL_DENSITY
        self.viscosity = kinematic_viscosity_m2_s or OIL_VISCOSITY
        self.interfacial_tension = interfacial_tension_n_m or INTERFACIAL_TENSION
        self.thickness = initial_thickness_m or 0.0001  # 0.1 mm default
    
    def to_dict(self):
        return {
            "density_kg_m3": self.density,
            "kinematic_viscosity_m2_s": self.viscosity,
            "interfacial_tension_n_m": self.interfacial_tension,
            "initial_thickness_m": self.thickness,
            "source": "default_generic" if self.density == OIL_DENSITY else "user_specified",
        }
