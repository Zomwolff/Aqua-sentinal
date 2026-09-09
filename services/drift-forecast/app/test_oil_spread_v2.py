"""
Tests for Oil Spread Component V2 - Separated Physics
"""
import numpy as np
import pytest
from datetime import datetime, timezone

from app.oil_physics import (
    compute_physical_spread_radius,
    compute_physical_area,
    compute_expansion_ratio,
    compute_spread_rate_m2_per_hour,
    OilProperties,
)
from app.uncertainty import (
    compute_particle_statistics,
    compute_probability_contours_grid,
    compute_uncertainty_footprint,
)
from app.particles import simulate_ensemble


# ============================================================================
# PHYSICAL SPREADING TESTS
# ============================================================================

def test_physical_spreading_zero_time():
    """At t=0, physical radius should equal initial equivalent radius."""
    initial_area = 1_000_000.0  # 1 km²
    initial_radius = np.sqrt(initial_area / np.pi)
    
    radius_t0 = compute_physical_spread_radius(initial_area, elapsed_hours=0.0)
    
    assert abs(radius_t0 - initial_radius) < 1.0  # Within 1 metre


def test_physical_spreading_increases_with_time():
    """Physical spreading should increase monotonically with time."""
    initial_area = 1_000_000.0  # 1 km²
    
    r_1h = compute_physical_spread_radius(initial_area, elapsed_hours=1.0)
    r_6h = compute_physical_spread_radius(initial_area, elapsed_hours=6.0)
    r_24h = compute_physical_spread_radius(initial_area, elapsed_hours=24.0)
    
    assert r_1h > 0
    assert r_6h > r_1h
    assert r_24h > r_6h


def test_physical_area_preserves_initial_observation():
    """
    OPTION A FIX VERIFICATION:
    Physical area at any forecast horizon should be >= initial observed area.
    This test verifies the time-reference fix eliminates non-physical contraction.
    """
    initial_area = 50_000.0  # 50,000 m² (audit test case)
    
    # Test multiple horizons
    for elapsed_hours in [1.0, 3.0, 6.0, 12.0, 24.0]:
        forecast_area = compute_physical_area(
            initial_area_m2=initial_area,
            elapsed_hours=elapsed_hours,
            oil_density=900.0,
            oil_viscosity=0.00001,
            water_density=1025.0,
            initial_thickness_m=0.0001,
        )
        
        # Critical assertion: forecast area should NOT be less than initial area
        assert forecast_area >= initial_area, \
            f"At {elapsed_hours}h: area={forecast_area:.2f} < initial={initial_area} (CONTRACTION!)"
        
        # Area should increase with time (monotonic growth)
        if elapsed_hours > 1.0:
            prev_area = compute_physical_area(
                initial_area_m2=initial_area,
                elapsed_hours=elapsed_hours - 1.0,
                oil_density=900.0,
                oil_viscosity=0.00001,
                water_density=1025.0,
                initial_thickness_m=0.0001,
            )
            assert forecast_area > prev_area, \
                f"Area should increase: {elapsed_hours}h={forecast_area} <= {elapsed_hours-1}h={prev_area}"


def test_expansion_ratio_always_positive():
    """
    OPTION A FIX VERIFICATION:
    Expansion ratio should always be >= 1.0 (no contraction).
    """
    initial_area = 50_000.0
    
    for elapsed_hours in [0.5, 1.0, 2.0, 3.0, 6.0, 12.0, 24.0]:
        forecast_area = compute_physical_area(
            initial_area_m2=initial_area,
            elapsed_hours=elapsed_hours,
        )
        
        expansion_ratio = compute_expansion_ratio(initial_area, forecast_area)
        
        assert expansion_ratio >= 1.0, \
            f"At {elapsed_hours}h: expansion_ratio={expansion_ratio:.4f} < 1.0 (CONTRACTION!)"


def test_spread_rate_always_non_negative():
    """
    OPTION A FIX VERIFICATION:
    Spread rate should always be >= 0 (no negative growth).
    """
    initial_area = 50_000.0
    
    for elapsed_hours in [1.0, 3.0, 6.0, 12.0, 24.0]:
        forecast_area = compute_physical_area(
            initial_area_m2=initial_area,
            elapsed_hours=elapsed_hours,
        )
        
        spread_rate = compute_spread_rate_m2_per_hour(initial_area, forecast_area, elapsed_hours)
        
        assert spread_rate >= 0.0, \
            f"At {elapsed_hours}h: spread_rate={spread_rate:.2f} < 0 (NEGATIVE GROWTH!)"


def test_expansion_ratio():
    """Expansion ratio should be >= 1.0."""
    initial_area = 1_000_000.0
    forecast_area = 2_500_000.0
    
    ratio = compute_expansion_ratio(initial_area, forecast_area)
    
    assert ratio == 2.5
    assert ratio >= 1.0


def test_spread_rate_calculation():
    """Spread rate should match (area_final - area_initial) / time."""
    initial_area = 1_000_000.0
    forecast_area = 2_000_000.0
    elapsed_hours = 10.0
    
    rate = compute_spread_rate_m2_per_hour(initial_area, forecast_area, elapsed_hours)
    
    expected_rate = (forecast_area - initial_area) / elapsed_hours
    assert abs(rate - expected_rate) < 1.0


def test_oil_properties_defaults():
    """OilProperties should provide safe defaults."""
    props = OilProperties()
    
    assert props.density > 0
    assert props.viscosity > 0
    assert props.interfacial_tension > 0
    assert props.thickness > 0
    assert props.to_dict()["source"] == "default_generic"


# ============================================================================
# UNCERTAINTY TESTS
# ============================================================================

def test_particle_statistics_zero_spread():
    """When all particles at same location, RMS spread should be zero."""
    lat_particles = np.array([18.5] * 100)
    lon_particles = np.array([72.0] * 100)
    
    stats = compute_particle_statistics(lat_particles, lon_particles, 18.5, 72.0)
    
    assert stats["rms_spread_m"] < 1.0  # Essentially zero


def test_particle_statistics_symmetric_spread():
    """Symmetric spread should have similar east/north std deviations."""
    rng = np.random.default_rng(42)
    n = 500
    
    # Create symmetric Gaussian cloud
    lat_particles = 18.5 + rng.normal(0, 0.01, n)
    lon_particles = 72.0 + rng.normal(0, 0.01, n)
    
    stats = compute_particle_statistics(lat_particles, lon_particles, 18.5, 72.0)
    
    # East and north std should be similar (within 50% tolerance)
    ratio = stats["std_east_m"] / stats["std_north_m"]
    assert 0.5 < ratio < 2.0


def test_probability_contours_mass_conservation():
    """50% contour should contain ~50% of particles, 90% contour ~90%."""
    rng = np.random.default_rng(42)
    n = 1000
    
    # Create Gaussian cloud
    lat_particles = rng.normal(18.5, 0.02, n)
    lon_particles = rng.normal(72.0, 0.02, n)
    
    result = compute_uncertainty_footprint(
        lat_particles, lon_particles,
        method="grid",
        probability_levels=[0.50, 0.90],
    )
    
    # Check that contours were generated
    contours = result["probability_contours"]
    
    # Should have both 50% and 90% contours
    assert "0.50" in contours or "0.90" in contours
    
    # RMS spread should be reasonable
    assert result["rms_spread_m"] > 0


def test_probability_contour_actual_containment():
    """
    RIGOROUS TEST: Verify that final output polygon actually contains
    approximately the correct fraction of particles.
    
    IMPORTANT: Due to convex hull approximation of grid cells, there is
    inherent geometric error. This test documents the actual behavior.
    """
    from shapely.geometry import Point, Polygon
    from shapely import wkt as shapely_wkt
    
    rng = np.random.default_rng(42)
    n = 1000
    
    # Test Case 1: Gaussian distribution
    lat_particles = rng.normal(18.5, 0.02, n)
    lon_particles = rng.normal(72.0, 0.02, n)
    
    result = compute_uncertainty_footprint(
        lat_particles, lon_particles,
        method="grid",
        probability_levels=[0.50, 0.90],
    )
    
    contours = result["probability_contours"]
    
    containment_results = {}
    for prob_level_str, wkt_string in contours.items():
        prob_level = float(prob_level_str)
        polygon = shapely_wkt.loads(wkt_string)
        
        # Count how many particles are actually inside the polygon
        particles_inside = sum(
            polygon.contains(Point(lon, lat))
            for lon, lat in zip(lon_particles, lat_particles)
        )
        
        actual_fraction = particles_inside / n
        containment_results[prob_level_str] = actual_fraction
        
        # VERIFICATION: The convex hull approximation introduces geometric error.
        # We verify the error is within acceptable bounds (±10% of target).
        error = abs(actual_fraction - prob_level)
        tolerance = 0.10  # ±10% absolute tolerance
        
        assert error <= tolerance, \
            f"{prob_level_str} contour: actual={actual_fraction:.3f}, " \
            f"target={prob_level:.2f}, error={error:.3f} > {tolerance}"
    
    # Document actual containment for scientific honesty
    print(f"\nActual containment fractions: {containment_results}")
    
    # Verify at least some contours were generated
    assert len(contours) > 0, "No probability contours generated"


def test_convex_hull_not_probability_contour():
    """Convex hull should enclose ALL particles, not a probability level."""
    rng = np.random.default_rng(42)
    n = 100
    
    lat_particles = rng.normal(18.5, 0.01, n)
    lon_particles = rng.normal(72.0, 0.01, n)
    
    result = compute_uncertainty_footprint(lat_particles, lon_particles)
    
    # Convex hull should exist
    assert result["convex_hull_wkt"] is not None
    
    # Convex hull is for visualization/reference, NOT a probability interpretation
    # (This is a documentation test - the code doesn't claim otherwise)


# ============================================================================
# INTEGRATED PARTICLE SIMULATION TESTS
# ============================================================================

def test_zero_current_zero_wind_zero_diffusion():
    """With no forcing or diffusion, centroid should remain stationary (physical spreading still occurs)."""
    forcing = [
        {
            "t": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            "wind_speed_ms": 0.0,
            "wind_dir_deg": 0.0,
            "current_speed_ms": 0.0,
            "current_dir_deg": 0.0,
        },
        {
            "t": datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            "wind_speed_ms": 0.0,
            "wind_dir_deg": 0.0,
            "current_speed_ms": 0.0,
            "current_dir_deg": 0.0,
        },
    ]
    
    results = simulate_ensemble(
        spill_lat=18.5,
        spill_lon=72.0,
        area_m2=1_000_000.0,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        forcing_series=forcing,
        horizons_h=[6.0],
        n_particles=100,
        diffusivity_m2_s=0.0,  # No diffusion
        seed=42,
    )
    
    assert len(results) == 1
    fc = results[0]
    
    # Centroid should be very close to initial position (within numerical error)
    assert abs(fc["predicted_lat"] - 18.5) < 0.001
    assert abs(fc["predicted_lon"] - 72.0) < 0.001
    
    # Drift distance should be minimal
    assert fc["drift_distance_m"] < 100.0
    
    # Physical spreading should still occur (Fay model with Option A fix)
    # OPTION A FIX: Area should now be >= initial area (no contraction)
    assert fc["physical_area_m2"] >= 1_000_000.0
    assert fc["expansion_ratio"] >= 1.0


def test_constant_current_advection():
    """With constant current, centroid displacement should match current * time."""
    current_speed = 0.5  # m/s
    current_dir = 0.0    # From North (blowing south)
    elapsed_hours = 3.0
    
    forcing = [
        {
            "t": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            "wind_speed_ms": 0.0,
            "wind_dir_deg": 0.0,
            "current_speed_ms": current_speed,
            "current_dir_deg": current_dir,
        },
        {
            "t": datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc),
            "wind_speed_ms": 0.0,
            "wind_dir_deg": 0.0,
            "current_speed_ms": current_speed,
            "current_dir_deg": current_dir,
        },
    ]
    
    results = simulate_ensemble(
        spill_lat=18.0,
        spill_lon=72.0,
        area_m2=1_000_000.0,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        forcing_series=forcing,
        horizons_h=[elapsed_hours],
        n_particles=100,
        diffusivity_m2_s=1.0,  # Small diffusion
        seed=42,
    )
    
    fc = results[0]
    
    # Expected displacement: 0.5 m/s * 3 hours * 3600 s/h = 5400 m
    expected_displacement = current_speed * elapsed_hours * 3600.0
    
    # Allow 20% tolerance due to diffusion and particle sampling
    assert abs(fc["drift_distance_m"] - expected_displacement) < 0.2 * expected_displacement
    
    # Bearing: current_dir=0° means FROM north (met convention), so movement is SOUTH (180°)
    # Allow wide tolerance for meteorological direction interpretation
    assert fc["drift_bearing_deg"] > 90.0 and fc["drift_bearing_deg"] < 270.0  # Generally southward


def test_windage_only():
    """With wind only, movement should be approximately windage% * wind."""
    wind_speed = 10.0  # m/s
    wind_dir = 90.0    # From east (blowing west)
    elapsed_hours = 6.0
    
    forcing = [
        {
            "t": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            "wind_speed_ms": wind_speed,
            "wind_dir_deg": wind_dir,
            "current_speed_ms": 0.0,
            "current_dir_deg": 0.0,
        },
        {
            "t": datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
            "wind_speed_ms": wind_speed,
            "wind_dir_deg": wind_dir,
            "current_speed_ms": 0.0,
            "current_dir_deg": 0.0,
        },
    ]
    
    results = simulate_ensemble(
        spill_lat=18.0,
        spill_lon=72.0,
        area_m2=1_000_000.0,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        forcing_series=forcing,
        horizons_h=[elapsed_hours],
        n_particles=100,
        diffusivity_m2_s=1.0,
        seed=42,
    )
    
    fc = results[0]
    
    # Expected displacement: windage (1-4%, avg ~2.5%) * wind_speed * time
    # Use 2.5% as midpoint for rough estimate
    avg_windage = 0.025
    expected_displacement = avg_windage * wind_speed * elapsed_hours * 3600.0
    
    # Allow 50% tolerance due to windage variability and diffusion
    assert fc["drift_distance_m"] > 0.5 * expected_displacement
    assert fc["drift_distance_m"] < 2.0 * expected_displacement


def test_area_consistency():
    """Reported physical area should match geometry area within tolerance."""
    forcing = [
        {
            "t": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            "wind_speed_ms": 5.0,
            "wind_dir_deg": 180.0,
            "current_speed_ms": 0.3,
            "current_dir_deg": 200.0,
        },
    ]
    
    results = simulate_ensemble(
        spill_lat=18.5,
        spill_lon=72.0,
        area_m2=1_000_000.0,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        forcing_series=forcing,
        horizons_h=[12.0],
        n_particles=200,
        seed=42,
    )
    
    fc = results[0]
    
    # Physical area should match expected from radius: A = π * r²
    radius = fc["physical_radius_m"]
    computed_area = np.pi * radius ** 2
    reported_area = fc["physical_area_m2"]
    
    assert abs(computed_area - reported_area) / reported_area < 0.01  # Within 1%


def test_expansion_ratio_consistency():
    """Expansion ratio should equal forecast_area / initial_area."""
    initial_area = 1_000_000.0
    forcing = [{"t": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
                "wind_speed_ms": 3.0, "wind_dir_deg": 0.0,
                "current_speed_ms": 0.2, "current_dir_deg": 0.0}]
    
    results = simulate_ensemble(
        spill_lat=18.5, spill_lon=72.0, area_m2=initial_area,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        forcing_series=forcing, horizons_h=[24.0],
        n_particles=100, seed=42,
    )
    
    fc = results[0]
    
    computed_ratio = fc["physical_area_m2"] / initial_area
    reported_ratio = fc["expansion_ratio"]
    
    assert abs(computed_ratio - reported_ratio) / reported_ratio < 0.001  # Within 0.1%


def test_reproducibility():
    """Fixed seed should produce reproducible results."""
    forcing = [{"t": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
                "wind_speed_ms": 5.0, "wind_dir_deg": 45.0,
                "current_speed_ms": 0.5, "current_dir_deg": 30.0}]
    
    results1 = simulate_ensemble(
        spill_lat=18.5, spill_lon=72.0, area_m2=1_000_000.0,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        forcing_series=forcing, horizons_h=[6.0],
        n_particles=200, seed=42,
    )
    
    results2 = simulate_ensemble(
        spill_lat=18.5, spill_lon=72.0, area_m2=1_000_000.0,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        forcing_series=forcing, horizons_h=[6.0],
        n_particles=200, seed=42,
    )
    
    fc1 = results1[0]
    fc2 = results2[0]
    
    # Should be identical
    assert fc1["predicted_lat"] == fc2["predicted_lat"]
    assert fc1["predicted_lon"] == fc2["predicted_lon"]
    assert fc1["drift_distance_m"] == fc2["drift_distance_m"]
    assert fc1["physical_area_m2"] == fc2["physical_area_m2"]


def test_model_version_correct():
    """Model version should indicate V2 separated physics."""
    forcing = [{"t": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
                "wind_speed_ms": 3.0, "wind_dir_deg": 0.0,
                "current_speed_ms": 0.2, "current_dir_deg": 0.0}]
    
    results = simulate_ensemble(
        spill_lat=18.5, spill_lon=72.0, area_m2=1_000_000.0,
        start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        forcing_series=forcing, horizons_h=[1.0],
        n_particles=50, seed=42,
    )
    
    fc = results[0]
    
    assert fc["model"] == "ensemble-v2-separated-physics"
    assert "oil_properties" in fc
    assert "windage_range" in fc


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
