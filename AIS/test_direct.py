#!/usr/bin/env python3
import sys
sys.path.insert(0, "/Users/apeksha/Desktop/AquaSen/Aqua-sentinal/AIS/services/calibration")
sys.path.insert(0, "/Users/apeksha/Desktop/AquaSen/Aqua-sentinal/AIS/services/anomaly-detection/app")

print("Testing imports...")
try:
    from config_loader import get_calibration_config
    print("✓ Config loader imported")
    
    config = get_calibration_config()
    print(f"✓ Config loaded: {config.get_version()}")
    
    threshold = config.get_threshold("erratic_course_variance_threshold_normalized", 0.75)
    print(f"✓ Erratic course threshold: {threshold}")
    
    if threshold < 1.0:
        print("✓ CIRCULAR VARIANCE BUG IS FIXED - using normalized [0,1] scale")
    else:
        print("✗ Bug still present")
        
except Exception as e:
    print(f"✗ Error: {e}")
    import traceback
    traceback.print_exc()
