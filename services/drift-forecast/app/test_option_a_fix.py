"""
Verification script for Option A fix from OIL_SPREAD_V2_PHYSICAL_SPREADING_AUDIT.md

This script demonstrates the before/after behavior for the audit test case:
- Initial observed area: 50,000 m²
- Oil properties: density=900, viscosity=0.00001, thickness=0.0001
- Forecast horizons: 1h, 3h, 6h, 12h, 24h

BEFORE Option A (time-reference mismatch):
- 1h: 35,294.68 m² (CONTRACTION!)
- expansion_ratio at 1h: 0.7059 (< 1.0 = non-physical)
- spread_rate at 1h: -14,705.32 m²/h (negative = non-physical)

AFTER Option A (differential spreading):
- All areas >= 50,000 m²
- All expansion_ratio >= 1.0
- All spread_rate >= 0
"""
from app.oil_physics import (
    compute_physical_area,
    compute_physical_spread_radius,
    compute_expansion_ratio,
    compute_spread_rate_m2_per_hour,
    estimate_effective_age_hours,
    estimate_volume_from_area_and_thickness,
)

# Audit test case parameters
INITIAL_AREA = 50_000.0  # m²
OIL_DENSITY = 900.0  # kg/m³
OIL_VISCOSITY = 0.00001  # m²/s (10 cSt)
WATER_DENSITY = 1025.0  # kg/m³
INITIAL_THICKNESS = 0.0001  # m (0.1 mm)

HORIZONS = [1.0, 3.0, 6.0, 12.0, 24.0]


def main():
    print("=" * 80)
    print("OPTION A FIX VERIFICATION")
    print("Test Case from OIL_SPREAD_V2_PHYSICAL_SPREADING_AUDIT.md")
    print("=" * 80)
    print()
    
    print("INITIAL CONDITIONS:")
    print(f"  Initial observed area: {INITIAL_AREA:,.2f} m²")
    print(f"  Oil density: {OIL_DENSITY} kg/m³")
    print(f"  Oil viscosity: {OIL_VISCOSITY} m²/s")
    print(f"  Initial thickness (assumed): {INITIAL_THICKNESS} m ({INITIAL_THICKNESS * 1000} mm)")
    print()
    
    # Estimate volume and effective age
    volume_m3 = estimate_volume_from_area_and_thickness(INITIAL_AREA, INITIAL_THICKNESS)
    t_effective_hours = estimate_effective_age_hours(
        observed_area_m2=INITIAL_AREA,
        volume_m3=volume_m3,
        oil_density=OIL_DENSITY,
        oil_viscosity=OIL_VISCOSITY,
        water_density=WATER_DENSITY,
    )
    
    print("OPTION A PARAMETERS:")
    print(f"  Estimated volume: {volume_m3:.2f} m³")
    print(f"  Estimated effective age at detection: {t_effective_hours:.3f} hours")
    print(f"  (This is how long the spill was spreading before SAR detected it)")
    print()
    
    print("=" * 80)
    print("FORECAST RESULTS WITH OPTION A FIX:")
    print("=" * 80)
    print()
    
    print(f"{'Horizon':<10} {'Area (m²)':<15} {'Radius (m)':<12} {'Expansion':<12} {'Rate (m²/h)':<15}")
    print(f"{'(hours)':<10} {'':15} {'':12} {'Ratio':12} {'':15}")
    print("-" * 80)
    
    # T=0 (initial condition)
    area_t0 = INITIAL_AREA
    radius_t0 = (area_t0 / 3.14159) ** 0.5
    print(f"{'0 (init)':<10} {area_t0:>14,.2f} {radius_t0:>11,.1f} {'1.0000':>11} {'0.00':>14}")
    
    results = []
    for h in HORIZONS:
        area = compute_physical_area(
            initial_area_m2=INITIAL_AREA,
            elapsed_hours=h,
            oil_density=OIL_DENSITY,
            oil_viscosity=OIL_VISCOSITY,
            water_density=WATER_DENSITY,
            initial_thickness_m=INITIAL_THICKNESS,
        )
        
        radius = compute_physical_spread_radius(
            initial_area_m2=INITIAL_AREA,
            elapsed_hours=h,
            oil_density=OIL_DENSITY,
            oil_viscosity=OIL_VISCOSITY,
            water_density=WATER_DENSITY,
            initial_thickness_m=INITIAL_THICKNESS,
        )
        
        expansion = compute_expansion_ratio(INITIAL_AREA, area)
        rate = compute_spread_rate_m2_per_hour(INITIAL_AREA, area, h)
        
        results.append({
            "horizon": h,
            "area": area,
            "radius": radius,
            "expansion": expansion,
            "rate": rate,
        })
        
        print(f"{h:<10.1f} {area:>14,.2f} {radius:>11,.1f} {expansion:>11.4f} {rate:>14,.2f}")
    
    print()
    print("=" * 80)
    print("VERIFICATION CHECKS:")
    print("=" * 80)
    print()
    
    all_passed = True
    
    # Check 1: No area contraction
    print("✓ CHECK 1: No area contraction (all areas >= initial area)")
    for r in results:
        if r["area"] < INITIAL_AREA:
            print(f"  ✗ FAILED at {r['horizon']}h: {r['area']:.2f} < {INITIAL_AREA}")
            all_passed = False
        else:
            print(f"  ✓ {r['horizon']}h: {r['area']:,.2f} >= {INITIAL_AREA:,.2f}")
    print()
    
    # Check 2: All expansion ratios >= 1.0
    print("✓ CHECK 2: No expansion ratio < 1.0 (all >= 1.0)")
    for r in results:
        if r["expansion"] < 1.0:
            print(f"  ✗ FAILED at {r['horizon']}h: {r['expansion']:.4f} < 1.0")
            all_passed = False
        else:
            print(f"  ✓ {r['horizon']}h: {r['expansion']:.4f} >= 1.0")
    print()
    
    # Check 3: No negative spread rates
    print("✓ CHECK 3: No negative spread rate (all >= 0)")
    for r in results:
        if r["rate"] < 0:
            print(f"  ✗ FAILED at {r['horizon']}h: {r['rate']:.2f} < 0")
            all_passed = False
        else:
            print(f"  ✓ {r['horizon']}h: {r['rate']:.2f} >= 0")
    print()
    
    # Check 4: Monotonic growth
    print("✓ CHECK 4: Monotonic area growth")
    for i in range(1, len(results)):
        if results[i]["area"] <= results[i-1]["area"]:
            print(f"  ✗ FAILED: {results[i]['horizon']}h area <= {results[i-1]['horizon']}h area")
            all_passed = False
        else:
            growth = results[i]["area"] - results[i-1]["area"]
            print(f"  ✓ {results[i-1]['horizon']}h → {results[i]['horizon']}h: +{growth:,.2f} m²")
    print()
    
    # Check 5: Radius consistency
    print("✓ CHECK 5: Radius consistency (r = √(A/π))")
    for r in results:
        expected_radius = (r["area"] / 3.14159) ** 0.5
        error = abs(r["radius"] - expected_radius)
        if error > 1.0:  # 1 meter tolerance
            print(f"  ✗ FAILED at {r['horizon']}h: radius error = {error:.2f} m")
            all_passed = False
        else:
            print(f"  ✓ {r['horizon']}h: radius error = {error:.3f} m (< 1 m)")
    print()
    
    print("=" * 80)
    if all_passed:
        print("✓✓✓ ALL VERIFICATION CHECKS PASSED ✓✓✓")
        print()
        print("Option A fix successfully eliminates:")
        print("  - Non-physical area contraction")
        print("  - Expansion ratios < 1.0")
        print("  - Negative spread rates")
        print()
        print("Physical spreading now:")
        print("  - Preserves observed initial area")
        print("  - Applies Fay differential spreading from baseline")
        print("  - Maintains monotonic growth")
    else:
        print("✗✗✗ SOME CHECKS FAILED ✗✗✗")
    print("=" * 80)
    print()
    
    print("COMPARISON WITH AUDIT REPORT (BEFORE Option A):")
    print("  Before Option A:")
    print("    1h:  35,294.68 m² (expansion_ratio=0.7059, rate=-14,705.32)")
    print("    3h:  61,132.17 m² (expansion_ratio=1.2226, rate=+3,710.72)")
    print("    6h:  86,453.95 m²")
    print("   12h: 122,264.35 m²")
    print("   24h: 172,907.90 m²")
    print()
    print("  After Option A:")
    for r in results:
        print(f"    {r['horizon']:>2.0f}h: {r['area']:>10,.2f} m² "
              f"(expansion_ratio={r['expansion']:.4f}, rate={r['rate']:+.2f})")
    print()


if __name__ == "__main__":
    main()
