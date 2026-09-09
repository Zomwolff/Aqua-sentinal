import redis
import json
import time

r = redis.Redis(host='localhost', port=6380, db=0)

mmsi = "419002033"
scene_id = "SCENE_TEST_888"

print(f"Publishing critical risk for MMSI {mmsi}...")
r.xadd("vessel.risk", {
    "mmsi": mmsi,
    "score": "95.0",
    "tier": "CRITICAL",
    "action": "Immediate SAR Tasking"
})

time.sleep(3)

steps = [
    "sar_tasking",
    "sar_fetching",
    "sar_despeckling",
    "sar_cfar",
    "sar_morphology",
    "sar_polygonize",
    "sar_complete"
]

for step in steps:
    print(f"Publishing {step}...")
    r.xadd("sar.tasking.events", {"scene_id": scene_id, "step": step, "candidates": str(3 if step == "sar_complete" else 0)})
    time.sleep(2)

print("Done")
