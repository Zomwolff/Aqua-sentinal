"""
End-to-End Test of Refactored AIS Anomaly Detection System

Tests:
1. Normal vessel behavior - should NOT be flagged
2. Sudden stop anomaly - should be flagged
3. Erratic course - should be flagged
4. Loitering - should be flagged
5. Speed anomaly - should be flagged

Uses synthetic Mumbai AIS data and generates abnormal scenarios.
"""
import csv
import sys
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

# Add paths
sys.path.insert(0, str(Path(__file__).parent / "services/anomaly-detection/app"))
sys.path.insert(0, str(Path(__file__).parent / "services/calibration"))
sys.path.insert(0, str(Path(__file__).parent / "services/shared"))

try:
    from rules import apply_rules, check_ais_gap
    from config_loader import get_calibration_config
    print("✓ Successfully imported anomaly detection modules")
except ImportError as e:
    print(f"✗ Import error: {e}")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# Test Data Generators
# ──────────────────────────────────────────────────────────────────────────────

def generate_normal_vessel_window() -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Generate normal vessel behavior features."""
    return (
        {
            "mmsi": 419900684,
            "window_start": datetime.now(timezone.utc).isoformat(),
            "avg_speed": 12.0,  # Normal cargo speed
            "course_variance": 0.2,  # Low variance (normalized)
            "loitering_score": 0.1,  # Not loitering
            "ping_count": 20,
            "mean_cog_heading_divergence_deg": 5.0,  # Small divergence
            "max_cog_heading_divergence_deg": 10.0,
            "repeated_cog_heading_divergences": 1,
            "max_rate_of_turn_deg_min": 5.0,
            "turn_reversal_count": 0,
            "draught_change_m": 0.0,
        },
        {
            "vessel_type": "cargo",
            "destination": "Mumbai",
            "last_lat": 18.936,
            "last_lon": 72.838,
            "last_course": 270.0,
        },
    )


def generate_sudden_stop_window() -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Generate sudden stop anomaly (vessel drops from 15kn to 0.2kn outside port)."""
    current = {
        "mmsi": 419059699,
        "window_start": datetime.now(timezone.utc).isoformat(),
        "avg_speed": 0.2,  # Nearly stopped
        "course_variance": 0.3,
        "loitering_score": 0.5,
        "ping_count": 10,
        "mean_cog_heading_divergence_deg": 8.0,
        "max_cog_heading_divergence_deg": 15.0,
        "repeated_cog_heading_divergences": 2,
        "max_rate_of_turn_deg_min": 3.0,
        "turn_reversal_count": 0,
        "draught_change_m": 0.0,
    }
    
    previous = {
        "mmsi": 419059699,
        "window_start": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "avg_speed": 15.0,  # Was moving fast
        "course_variance": 0.15,
        "loitering_score": 0.05,
        "ping_count": 20,
    }
    
    vessel_meta = {
        "vessel_type": "fishing",
        "destination": None,
        "last_lat": 19.5,  # Away from port
        "last_lon": 72.5,
        "last_course": 180.0,
    }
    
    return current, previous, vessel_meta


def generate_erratic_course_window() -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Generate erratic course anomaly (high variance in heading)."""
    return (
        {
            "mmsi": 352002662,
            "window_start": datetime.now(timezone.utc).isoformat(),
            "avg_speed": 8.0,
            "course_variance": 0.92,  # Very high variance (should be > 0.75 threshold)
            "loitering_score": 0.3,
            "ping_count": 15,
            "mean_cog_heading_divergence_deg": 35.0,
            "max_cog_heading_divergence_deg": 60.0,
            "repeated_cog_heading_divergences": 5,
            "max_rate_of_turn_deg_min": 25.0,  # High turn rate
            "turn_reversal_count": 4,
            "draught_change_m": 0.0,
        },
        {
            "vessel_type": "tanker",
            "destination": None,
            "last_lat": 18.9,
            "last_lon": 72.9,
            "last_course": 45.0,
        },
    )


def generate_loitering_window() -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Generate loitering anomaly (sustained low speed mid-ocean)."""
    return (
        {
            "mmsi": 211378120,
            "window_start": datetime.now(timezone.utc).isoformat(),
            "avg_speed": 0.5,  # Very slow
            "course_variance": 0.4,
            "loitering_score": 0.95,  # High loitering (> 0.8 threshold)
            "ping_count": 25,
            "mean_cog_heading_divergence_deg": 10.0,
            "max_cog_heading_divergence_deg": 20.0,
            "repeated_cog_heading_divergences": 2,
            "max_rate_of_turn_deg_min": 5.0,
            "turn_reversal_count": 0,
            "draught_change_m": 0.0,
        },
        {
            "vessel_type": "tanker",
            "destination": "Singapore",
            "last_lat": 15.0,  # Mid-ocean, not in port
            "last_lon": 75.0,
            "last_course": 180.0,
        },
    )


def generate_speed_anomaly_window() -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Generate speed anomaly (vessel exceeds type max speed)."""
    return (
        {
            "mmsi": 235090000,
            "window_start": datetime.now(timezone.utc).isoformat(),
            "avg_speed": 35.0,  # Cargo max is ~25kn, this is 1.4x
            "course_variance": 0.25,
            "loitering_score": 0.1,
            "ping_count": 20,
            "mean_cog_heading_divergence_deg": 5.0,
            "max_cog_heading_divergence_deg": 10.0,
            "repeated_cog_heading_divergences": 1,
            "max_rate_of_turn_deg_min": 3.0,
            "turn_reversal_count": 0,
            "draught_change_m": 0.0,
        },
        {
            "vessel_type": "cargo",
            "destination": "Kandla",
            "last_lat": 20.0,
            "last_lon": 71.0,
            "last_course": 270.0,
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Test Runner
# ──────────────────────────────────────────────────────────────────────────────

def print_test_header(title: str):
    """Print test section header."""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")


def print_result(passed: bool, message: str):
    """Print test result."""
    symbol = "✓" if passed else "✗"
    status = "PASS" if passed else "FAIL"
    print(f"{symbol} [{status}] {message}")


def test_normal_vessel():
    """Test that normal vessel behavior is NOT flagged."""
    print_test_header("TEST 1: Normal Vessel Behavior")
    
    features, vessel_meta = generate_normal_vessel_window()
    events = apply_rules(features, vessel_meta)
    
    print(f"MMSI: {features['mmsi']}")
    print(f"Speed: {features['avg_speed']} knots")
    print(f"Course Variance: {features['course_variance']:.3f} (threshold: 0.75)")
    print(f"Loitering Score: {features['loitering_score']:.3f} (threshold: 0.8)")
    
    if len(events) == 0:
        print_result(True, "Normal vessel correctly NOT flagged")
        return True
    else:
        print_result(False, f"Normal vessel incorrectly flagged: {events}")
        return False


def test_sudden_stop():
    """Test that sudden stop is flagged."""
    print_test_header("TEST 2: Sudden Stop Anomaly")
    
    current, previous, vessel_meta = generate_sudden_stop_window()
    events = apply_rules(current, vessel_meta, previous_features=previous, port_nearby=False)
    
    print(f"MMSI: {current['mmsi']}")
    print(f"Previous Speed: {previous['avg_speed']} knots")
    print(f"Current Speed: {current['avg_speed']} knots")
    print(f"Speed Drop: {previous['avg_speed'] - current['avg_speed']:.1f} knots")
    
    sudden_stop_detected = any(e.get("anomaly_type") == "sudden_stop" for e in events)
    if sudden_stop_detected:
        event = next(e for e in events if e.get("anomaly_type") == "sudden_stop")
        print_result(True, f"Sudden stop DETECTED (severity: {event.get('severity')}, confidence: {event.get('confidence', 'N/A')})")
        return True
    else:
        print_result(False, "Sudden stop NOT detected")
        return False


def test_erratic_course():
    """Test that erratic course is flagged."""
    print_test_header("TEST 3: Erratic Course Anomaly")
    
    features, vessel_meta = generate_erratic_course_window()
    events = apply_rules(features, vessel_meta)
    
    print(f"MMSI: {features['mmsi']}")
    print(f"Course Variance: {features['course_variance']:.3f} (threshold: 0.75)")
    print(f"Max Turn Rate: {features['max_rate_of_turn_deg_min']}°/min (threshold: 20°/min)")
    print(f"Turn Reversals: {features['turn_reversal_count']} (threshold: 3)")
    
    erratic_detected = any(
        e.get("anomaly_type") in ("erratic_course", "erratic_turn") 
        for e in events
    )
    if erratic_detected:
        anomaly_types = [e.get("anomaly_type") for e in events if e.get("anomaly_type") in ("erratic_course", "erratic_turn")]
        print_result(True, f"Erratic behavior DETECTED ({', '.join(anomaly_types)})")
        return True
    else:
        print_result(False, "Erratic behavior NOT detected")
        return False


def test_loitering():
    """Test that loitering is flagged."""
    print_test_header("TEST 4: Loitering Anomaly")
    
    features, vessel_meta = generate_loitering_window()
    events = apply_rules(features, vessel_meta, port_nearby=False)
    
    print(f"MMSI: {features['mmsi']}")
    print(f"Vessel Type: {vessel_meta['vessel_type']}")
    print(f"Speed: {features['avg_speed']} knots (very slow)")
    print(f"Loitering Score: {features['loitering_score']:.3f} (threshold: 0.8)")
    print(f"Location: ({vessel_meta['last_lat']}, {vessel_meta['last_lon']}) - away from port")
    
    loitering_detected = any(e.get("anomaly_type") == "loitering_anomaly" for e in events)
    if loitering_detected:
        event = next(e for e in events if e.get("anomaly_type") == "loitering_anomaly")
        print_result(True, f"Loitering DETECTED (severity: {event.get('severity')}, vessel_type: {vessel_meta['vessel_type']})")
        return True
    else:
        print_result(False, "Loitering NOT detected")
        return False


def test_speed_anomaly():
    """Test that speed anomaly is flagged."""
    print_test_header("TEST 5: Speed Anomaly")
    
    features, vessel_meta = generate_speed_anomaly_window()
    events = apply_rules(features, vessel_meta)
    
    print(f"MMSI: {features['mmsi']}")
    print(f"Vessel Type: {vessel_meta['vessel_type']}")
    print(f"Speed: {features['avg_speed']} knots")
    print(f"Type Max Speed: 25 knots")
    print(f"Ratio: {features['avg_speed']/25:.2f}x (threshold: 1.2x for MEDIUM, 1.5x for HIGH)")
    
    speed_detected = any(e.get("anomaly_type") == "speed_anomaly" for e in events)
    if speed_detected:
        event = next(e for e in events if e.get("anomaly_type") == "speed_anomaly")
        print_result(True, f"Speed anomaly DETECTED (severity: {event.get('severity')}, confidence: {event.get('confidence', 'N/A')})")
        return True
    else:
        print_result(False, "Speed anomaly NOT detected")
        return False


def test_real_ais_data():
    """Test with real synthetic Mumbai AIS data."""
    print_test_header("TEST 6: Real Synthetic AIS Data from Repository")
    
    csv_path = Path(__file__).parent / "synthetic-mumbai-ais.csv"
    
    if not csv_path.exists():
        print_result(False, f"CSV file not found: {csv_path}")
        return False
    
    print(f"Loading AIS data from: {csv_path}")
    
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        
        print(f"Loaded {len(rows)} AIS records")
        
        # Group by MMSI and analyze
        by_mmsi = {}
        for row in rows:
            mmsi = row['MMSI']
            if mmsi not in by_mmsi:
                by_mmsi[mmsi] = []
            by_mmsi[mmsi].append(row)
        
        print(f"Found {len(by_mmsi)} unique vessels")
        
        # Test a few vessels
        test_count = min(3, len(by_mmsi))
        for i, (mmsi, vessel_records) in enumerate(list(by_mmsi.items())[:test_count]):
            print(f"\n  Vessel {i+1}: MMSI {mmsi} ({len(vessel_records)} pings)")
            
            # Calculate basic features from records
            speeds = [float(r.get('SOG', 0)) for r in vessel_records]
            avg_speed = sum(speeds) / len(speeds) if speeds else 0
            max_speed = max(speeds) if speeds else 0
            
            print(f"    Avg Speed: {avg_speed:.1f} knots")
            print(f"    Max Speed: {max_speed:.1f} knots")
            print(f"    Records: {len(vessel_records)}")
        
        print_result(True, "Real AIS data loaded and analyzed successfully")
        return True
        
    except Exception as e:
        print_result(False, f"Error processing AIS data: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_ais_gap():
    """Test AIS gap detection."""
    print_test_header("TEST 7: AIS Gap Detection")
    
    mmsi = "419900684"
    last_seen = datetime.now(timezone.utc) - timedelta(minutes=120)  # 2 hours gap
    vessel_meta = {
        "vessel_type": "cargo",
        "last_lat": 18.936,
        "last_lon": 72.838,
    }
    
    event = check_ais_gap(mmsi, last_seen, vessel_meta, gap_threshold_minutes=90)
    
    print(f"MMSI: {mmsi}")
    print(f"Last Seen: {(datetime.now(timezone.utc) - last_seen).total_seconds() / 60:.0f} minutes ago")
    print(f"Threshold: 90 minutes")
    
    if event:
        print_result(True, f"AIS gap DETECTED (severity: {event.get('severity')}, confidence: {event.get('confidence', 'N/A')})")
        return True
    else:
        print_result(False, "AIS gap NOT detected")
        return False


def test_calibration_config():
    """Test that calibration config is loaded correctly."""
    print_test_header("TEST 8: Calibration Configuration")
    
    try:
        config = get_calibration_config()
        print(f"Config Version: {config.get_version()}")
        print(f"Thresholds:")
        print(f"  - Erratic Course Variance: {config.get_threshold('erratic_course_variance_threshold_normalized', 0.75):.3f} (FIXED: normalized [0,1], not 2500 degrees²)")
        print(f"  - AIS Gap: {config.get_threshold('ais_gap_threshold_minutes', 90)} minutes")
        print(f"  - Loitering: {config.get_threshold('loitering_score_threshold', 0.8):.2f}")
        print(f"  - Sudden Stop: {config.get_threshold('sudden_stop_speed_threshold_kn', 0.7):.2f} knots")
        print(f"Prior P(threat): {config.get_prior_threat_probability():.2f}")
        
        print_result(True, "Calibration config loaded successfully")
        return True
    except Exception as e:
        print_result(False, f"Error loading calibration config: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Main Test Runner
# ──────────────────────────────────────────────────────────────────────────────

def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("  AQUA-SENTINEL: REFACTORED AIS ANOMALY DETECTION - END-TO-END TEST")
    print("="*80)
    
    results = []
    
    # Test calibration config
    results.append(("Calibration Config", test_calibration_config()))
    
    # Test anomalies
    results.append(("Normal Vessel", test_normal_vessel()))
    results.append(("Sudden Stop", test_sudden_stop()))
    results.append(("Erratic Course", test_erratic_course()))
    results.append(("Loitering", test_loitering()))
    results.append(("Speed Anomaly", test_speed_anomaly()))
    results.append(("AIS Gap", test_ais_gap()))
    results.append(("Real AIS Data", test_real_ais_data()))
    
    # Summary
    print("\n" + "="*80)
    print("  TEST SUMMARY")
    print("="*80)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        symbol = "✓" if result else "✗"
        print(f"{symbol} {test_name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n✅ ALL TESTS PASSED - Refactored system is working correctly!")
        return 0
    else:
        print(f"\n❌ {total - passed} test(s) failed - review anomaly detection logic")
        return 1


if __name__ == "__main__":
    sys.exit(main())
