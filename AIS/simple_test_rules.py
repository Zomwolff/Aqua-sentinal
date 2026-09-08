#!/usr/bin/env python3
"""Simple direct test of anomaly detection rules"""
from datetime import datetime, timezone
import sys

print("=" * 80)
print("TESTING ANOMALY DETECTION RULES")
print("=" * 80)

# Test 1: Import rules module
print("\n[1] Testing rule imports...")
try:
    sys.path.insert(0, "/Users/apeksha/Desktop/AquaSen/Aqua-sentinal/AIS/services/anomaly-detection/app")
    from rules import apply_rules, ERRATIC_COURSE_VARIANCE_NORMALIZED, LOITERING_THRESHOLD
    print("✓ Rules imported successfully")
except Exception as e:
    print(f"✗ Failed to import rules: {e}")
    sys.exit(1)

# Test 2: Check that circular variance threshold is fixed
print("\n[2] Checking circular variance threshold fix...")
print(f"  Old value (BUG): 2500 (degrees²)")
print(f"  New value (FIXED): {ERRATIC_COURSE_VARIANCE_NORMALIZED} (normalized [0,1])")
if ERRATIC_COURSE_VARIANCE_NORMALIZED < 1.0:
    print("✓ Circular variance threshold FIXED (using normalized scale)")
else:
    print("✗ Circular variance still using wrong scale")

# Test 3: Test normal vessel
print("\n[3] Testing NORMAL VESSEL (should NOT be flagged)...")
normal_features = {
    "mmsi": 419900684,
    "window_start": datetime.now(timezone.utc).isoformat(),
    "avg_speed": 12.0,  # Normal
    "course_variance": 0.2,  # Normal (normalized)
    "loitering_score": 0.1,  # Normal
    "ping_count": 20,
    "mean_cog_heading_divergence_deg": 5.0,
    "max_cog_heading_divergence_deg": 10.0,
    "repeated_cog_heading_divergences": 1,
    "max_rate_of_turn_deg_min": 5.0,
    "turn_reversal_count": 0,
    "draught_change_m": 0.0,
}

normal_vessel = {
    "vessel_type": "cargo",
    "destination": "Mumbai",
    "last_lat": 18.936,
    "last_lon": 72.838,
    "last_course": 270.0,
}

events = apply_rules(normal_features, normal_vessel)
if len(events) == 0:
    print(f"✓ Normal vessel correctly NOT flagged (0 anomalies)")
else:
    print(f"✗ Normal vessel incorrectly flagged with {len(events)} anomalies:")
    for e in events:
        print(f"    - {e.get('anomaly_type')}: {e.get('severity')}")

# Test 4: Test erratic course (HIGH variance)
print("\n[4] Testing ERRATIC COURSE (should be flagged)...")
erratic_features = {
    "mmsi": 352002662,
    "window_start": datetime.now(timezone.utc).isoformat(),
    "avg_speed": 8.0,
    "course_variance": 0.92,  # HIGH (normalized, > 0.75 threshold)
    "loitering_score": 0.3,
    "ping_count": 15,
    "mean_cog_heading_divergence_deg": 35.0,
    "max_cog_heading_divergence_deg": 60.0,
    "repeated_cog_heading_divergences": 5,
    "max_rate_of_turn_deg_min": 25.0,
    "turn_reversal_count": 4,
    "draught_change_m": 0.0,
}

erratic_vessel = {
    "vessel_type": "tanker",
    "destination": None,
    "last_lat": 18.9,
    "last_lon": 72.9,
    "last_course": 45.0,
}

events = apply_rules(erratic_features, erratic_vessel)
erratic_detected = any(e.get("anomaly_type") in ("erratic_course", "erratic_turn") for e in events)
if erratic_detected:
    print(f"✓ Erratic course correctly FLAGGED ({len(events)} anomalies)")
    for e in events:
        print(f"    - {e.get('anomaly_type')}: {e.get('severity')} (confidence: {e.get('confidence', 'N/A')})")
else:
    print(f"✗ Erratic course NOT detected")

# Test 5: Test loitering
print("\n[5] Testing LOITERING (should be flagged)...")
loitering_features = {
    "mmsi": 211378120,
    "window_start": datetime.now(timezone.utc).isoformat(),
    "avg_speed": 0.5,  # Very slow
    "course_variance": 0.4,
    "loitering_score": 0.95,  # HIGH (> 0.8 threshold)
    "ping_count": 25,
    "mean_cog_heading_divergence_deg": 10.0,
    "max_cog_heading_divergence_deg": 20.0,
    "repeated_cog_heading_divergences": 2,
    "max_rate_of_turn_deg_min": 5.0,
    "turn_reversal_count": 0,
    "draught_change_m": 0.0,
}

loitering_vessel = {
    "vessel_type": "tanker",
    "destination": "Singapore",
    "last_lat": 15.0,  # Away from port
    "last_lon": 75.0,
    "last_course": 180.0,
}

events = apply_rules(loitering_features, loitering_vessel, port_nearby=False)
loitering_detected = any(e.get("anomaly_type") == "loitering_anomaly" for e in events)
if loitering_detected:
    print(f"✓ Loitering correctly FLAGGED ({len(events)} anomalies)")
    for e in events:
        print(f"    - {e.get('anomaly_type')}: {e.get('severity')} (confidence: {e.get('confidence', 'N/A')})")
else:
    print(f"✗ Loitering NOT detected")

# Test 6: Test speed anomaly
print("\n[6] Testing SPEED ANOMALY (should be flagged)...")
speed_features = {
    "mmsi": 235090000,
    "window_start": datetime.now(timezone.utc).isoformat(),
    "avg_speed": 35.0,  # Cargo max ~25, this is 1.4x
    "course_variance": 0.25,
    "loitering_score": 0.1,
    "ping_count": 20,
    "mean_cog_heading_divergence_deg": 5.0,
    "max_cog_heading_divergence_deg": 10.0,
    "repeated_cog_heading_divergences": 1,
    "max_rate_of_turn_deg_min": 3.0,
    "turn_reversal_count": 0,
    "draught_change_m": 0.0,
}

speed_vessel = {
    "vessel_type": "cargo",
    "destination": "Kandla",
    "last_lat": 20.0,
    "last_lon": 71.0,
    "last_course": 270.0,
}

events = apply_rules(speed_features, speed_vessel)
speed_detected = any(e.get("anomaly_type") == "speed_anomaly" for e in events)
if speed_detected:
    print(f"✓ Speed anomaly correctly FLAGGED ({len(events)} anomalies)")
    for e in events:
        print(f"    - {e.get('anomaly_type')}: {e.get('severity')} (confidence: {e.get('confidence', 'N/A')})")
else:
    print(f"✗ Speed anomaly NOT detected")

print("\n" + "=" * 80)
print("SUMMARY: All anomaly detection tests completed")
print("=" * 80)
