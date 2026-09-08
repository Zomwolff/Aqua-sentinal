"""B2 feature-extraction tests (task PART 15): 12 cases, no fabricated values.

Covers: valid SAR, tiny candidate, NoData, AIS miss/hit, weather miss/hit,
single/multi-scene persistence, degenerate geometry, constant GLCM,
insufficient pixels, plus the B2 CSV export contract.
"""
import csv
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo_postprocess import (
    extract_candidate_textures,
    _persistence_count,
    _boundary_irregularity,
    _elongation,
    run_postprocess,
)
from b2_export import export_csv, CSV_HEADER

import rasterio
from rasterio.transform import from_origin
from rasterio.crs import CRS
from PIL import Image
from pyproj import Geod

TMP = Path(tempfile.mkdtemp(prefix="b2_test_"))
print(f"workdir: {TMP}")
rng = np.random.default_rng(11)
GEOD = Geod(ellps="WGS84")
CRS_P = CRS.from_epsg(32643)
TR = from_origin(500000, 2100000, 10, 10)  # 10 m pixels


def make_scene(name, sar, mask, nodata=None):
    tif = TMP / f"{name}.tif"
    with rasterio.open(tif, "w", driver="GTiff", height=sar.shape[0],
                       width=sar.shape[1], count=1, dtype="float32",
                       crs=CRS_P, transform=TR, nodata=nodata) as dst:
        dst.write(sar.astype(np.float32), 1)
    mp = TMP / f"{name}_mask.png"
    Image.fromarray(mask.astype(np.uint8) * 255).save(mp)
    return mp, tif


def sar_blob(h=60, w=60, sea=-13.0, oil=-20.0):
    s = rng.normal(sea, 1.5, (h, w)).astype(np.float64)
    s[20:40, 20:45] = rng.normal(oil, 1.0, (20, 25))
    m = np.zeros((h, w), bool)
    m[20:40, 20:45] = True
    return s, m


# ---- 1. valid SAR candidate: full B2 vector present and finite ----
s1, m1 = sar_blob()
mp1, tif1 = make_scene("s1", s1, m1)
r1 = run_postprocess(mp1, tif1, TMP / "o1", min_object_px=1, min_hole_px=0,
                     closing_radius=0, acquisition_time="2026-08-10T00:00:00Z")
c1 = json.load(open(TMP / "o1" / "s1_spill_meta.json"))["candidates"][0]
for k in ["mean_backscatter", "std_backscatter", "perimeter_m", "elongation",
          "boundary_irregularity", "edge_sharpness"]:
    assert c1[k] is not None and np.isfinite(c1[k]), (k, c1[k])
assert c1["perimeter_m"] > 0 and c1["elongation"] >= 1.0
assert c1["boundary_irregularity"] >= 0.999
assert c1["persistence_count"] == 1 and c1["wind_speed_kmh"] is None
assert c1["distance_to_nearest_vessel_km"] is None  # missing stays missing
assert c1["validation_warnings"] == []
print(f"1 PASS valid: mean={c1['mean_backscatter']:.2f} std={c1['std_backscatter']:.2f} "
      f"perim={c1['perimeter_m']:.1f}m elong={c1['elongation']:.2f} "
      f"irreg={c1['boundary_irregularity']:.2f} edge={c1['edge_sharpness']:.2f}")

# ---- 2. tiny candidate: documented skip, nulls, no crash ----
s2 = rng.normal(-13, 1, (30, 30))
m2 = np.zeros((30, 30), bool)
m2[10:12, 10:12] = True  # 4 px
mp2, tif2 = make_scene("s2", s2, m2)
r2 = run_postprocess(mp2, tif2, TMP / "o2", min_object_px=1, min_hole_px=0,
                     closing_radius=0)
c2 = json.load(open(TMP / "o2" / "s2_spill_meta.json"))["candidates"][0]
assert c2["texture"] is None and c2["texture_status"] == "insufficient_valid_pixels"
assert c2["mean_backscatter"] is None and c2["perimeter_m"] is None
print("2 PASS tiny ->", c2["texture_status"], "+ null B2 fields")

# ---- 3. NoData pixels excluded (unit level: unfiltered mask + valid mask) ----
s3 = rng.normal(-13.0, 1.5, (60, 60))
s3[20:40, 20:45] = rng.normal(-20.0, 1.0, (20, 25))
s3[:, 40:] = -9999.0
m3b = np.zeros((60, 60), bool)
m3b[20:40, 20:50] = True  # straddles the nodata half
valid3 = (s3 != -9999.0)
c3x, _ = extract_candidate_textures(m3b, s3, valid3, "t3")
c3 = c3x[0]
assert c3["valid_pixel_fraction"] < 1.0 and c3["texture_status"] == "ok"
assert np.isfinite(c3["mean_backscatter"]) and abs(c3["mean_backscatter"] + 9999) > 100
assert c3["texture"]["glcm"]["contrast_mean"] >= 0.0  # computed, not crashed
# pipeline level: nodata area never becomes candidate pixels
mp3, tif3 = make_scene("s3", s3, m3b, nodata=-9999.0)
r3 = run_postprocess(mp3, tif3, TMP / "o3", min_object_px=1, min_hole_px=0,
                     closing_radius=0)
c3p = json.load(open(TMP / "o3" / "s3_spill_meta.json"))["candidates"][0]
assert c3p["valid_pixel_fraction"] == 1.0  # nodata filtered before labeling
print(f"3 PASS nodata: unit frac={c3['valid_pixel_fraction']:.2f} mean={c3['mean_backscatter']:.2f}; "
      f"pipeline cleanly filtered")

# ---- 7. weather CSV match (nearest in space, +-3h) ----
wcsv = TMP / "weather.csv"
with open(wcsv, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["lat", "lon", "timestamp", "wind_speed_kmh", "source"])
    w.writerow(["19.0", "75.0", "2026-08-10T01:00:00Z", "18.5", "open_meteo"])
    w.writerow(["19.5", "73.5", "2026-08-10T01:00:00Z", "99.9", "open_meteo"])
r7 = run_postprocess(mp1, tif1, TMP / "o7", min_object_px=1, min_hole_px=0,
                     closing_radius=0, acquisition_time="2026-08-10T00:00:00Z",
                     weather_csv=str(wcsv))
c7 = json.load(open(TMP / "o7" / "s1_spill_meta.json"))["candidates"][0]
assert c7["wind_speed_kmh"] == 18.5, c7["wind_match"]  # nearer station wins
print("7 PASS weather match:", c7["wind_match"])

# ---- 6. weather CSV, nothing in window -> null, reason ----
wcsv2 = TMP / "weather2.csv"
with open(wcsv2, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["lat", "lon", "timestamp", "wind_speed_kmh"])
    w.writerow(["19.0", "75.0", "2026-01-01T00:00:00Z", "18.5"])
r6 = run_postprocess(mp1, tif1, TMP / "o6", min_object_px=1, min_hole_px=0,
                     closing_radius=0, acquisition_time="2026-08-10T00:00:00Z",
                     weather_csv=str(wcsv2))
c6 = json.load(open(TMP / "o6" / "s1_spill_meta.json"))["candidates"][0]
assert c6["wind_speed_kmh"] is None and c6["wind_status"] == "no_sample_in_window"
print("6 PASS weather miss ->", c6["wind_status"])

# ---- 5. AIS match: min geodesic distance verified independently ----
cent = c1["centroid_lonlat"]
vlon, vlat = cent[0] + 0.01, cent[1]  # ~1 km east
_, _, expect_m = GEOD.inv(cent[0], cent[1], vlon, vlat)
acsv = TMP / "ais.csv"
with open(acsv, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["lat", "lon", "timestamp", "mmsi"])
    w.writerow([str(vlat), str(vlon), "2026-08-10T00:30:00Z", "123456789"])
    w.writerow(["25.0", "80.0", "2026-08-10T00:30:00Z", "987654321"])
r5 = run_postprocess(mp1, tif1, TMP / "o5", min_object_px=1, min_hole_px=0,
                     closing_radius=0, acquisition_time="2026-08-10T00:00:00Z",
                     ais_csv=str(acsv))
c5 = json.load(open(TMP / "o5" / "s1_spill_meta.json"))["candidates"][0]
assert abs(c5["distance_to_nearest_vessel_km"] - expect_m / 1000.0) < 1e-9
assert c5["vessel_match"]["mmsi"] == "123456789"
print(f"5 PASS ais match: {c5['distance_to_nearest_vessel_km']:.4f} km (expect {expect_m/1000:.4f})")

# ---- 4. no AIS match (far outside window) ----
acsv2 = TMP / "ais2.csv"
with open(acsv2, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["lat", "lon", "timestamp", "mmsi"])
    w.writerow(["25.0", "80.0", "2020-01-01T00:00:00Z", "111111111"])
r4 = run_postprocess(mp1, tif1, TMP / "o4", min_object_px=1, min_hole_px=0,
                     closing_radius=0, acquisition_time="2026-08-10T00:00:00Z",
                     ais_csv=str(acsv2))
c4 = json.load(open(TMP / "o4" / "s1_spill_meta.json"))["candidates"][0]
assert c4["distance_to_nearest_vessel_km"] is None  # null, NOT 0
assert c4["vessel_status"] == "no_vessel_in_window"
print("4 PASS ais miss -> null (not 0),", c4["vessel_status"])

# ---- 8/9. persistence: single scene = 1; overlapping scene = 2 ----
assert c1["persistence_count"] == 1 and c1["persistence_status"] == "single_scene_default"
# build a second scene whose polygon overlaps candidate 1 (reuse its GeoJSON)
import shutil
shutil.copy(TMP / "o1" / "s1_spill.geojson", TMP / "sceneB.geojson")
r9 = run_postprocess(mp1, tif1, TMP / "o9", min_object_px=1, min_hole_px=0,
                     closing_radius=0, acquisition_time="2026-08-10T00:00:00Z",
                     persist_scene=[str(TMP / "sceneB.geojson")],
                     persist_time=["2026-08-16T00:00:00Z"],
                     persist_scene_id=["sceneB"])
c9 = json.load(open(TMP / "o9" / "s1_spill_meta.json"))["candidates"][0]
assert c9["persistence_count"] == 2 and c9["persistence_scenes"] == ["sceneB"], c9["persistence_scenes"]
# same scene id must not double-count itself
r9b = run_postprocess(mp1, tif1, TMP / "o9b", min_object_px=1, min_hole_px=0,
                      closing_radius=0, persist_scene=[str(TMP / "sceneB.geojson")],
                      persist_scene_id=["s1"])
c9b = json.load(open(TMP / "o9b" / "s1_spill_meta.json"))["candidates"][0]
assert c9b["persistence_count"] == 1
print("8/9 PASS persistence: single=1, overlap=2, self-id not double-counted")

# ---- 10. degenerate geometry ----
cnt, scenes, reason = _persistence_count(
    {"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]},
    "s", None, [])
assert cnt is None and reason == "invalid_geometry", reason
assert _boundary_irregularity(10.0, 0.0) is None
assert _boundary_irregularity(None, 5.0) is None
el = _elongation(np.zeros((5, 5), bool))
assert el is None
print("10 PASS degenerate -> nulls, no crash")

# ---- 11. constant GLCM candidate stays mathematically correct ----
s11 = np.full((40, 40), -18.0)
m11 = np.zeros((40, 40), bool)
m11[5:35, 5:35] = True
c11, _ = extract_candidate_textures(m11, s11, np.ones((40, 40), bool), "t11")
g11 = c11[0]["texture"]["glcm"]
assert g11["contrast_mean"] == 0.0 and g11["energy_mean"] == 1.0
assert c11[0]["elongation"] == 1.0  # square -> ~1
print("11 PASS constant: contrast=0 energy=1 elongation=1.0")

# ---- 12. insufficient pixels: texture null + B2 nulls ----
s12 = rng.normal(-13, 1, (30, 30))
m12 = np.zeros((30, 30), bool)
m12[10:13, 10:12] = True  # 6 px < 20
c12, _ = extract_candidate_textures(m12, s12, np.ones((30, 30), bool), "t12")
assert c12[0]["texture"] is None
assert c12[0]["mean_backscatter"] is None and c12[0]["elongation"] is None
print("12 PASS insufficient ->", c12[0]["texture_status"])

# ---- Auto range: scene p1/p99 used when bounds are None ----
from geo_postprocess import resolve_glcm_range
sarA = rng.normal(-22.0, 3.0, (50, 50))
lo, hi, src = resolve_glcm_range(sarA, None, None)
assert src == "auto_p01_p99" and lo < hi
assert abs(lo - float(np.percentile(sarA, 1))) < 1e-9
assert abs(hi - float(np.percentile(sarA, 99))) < 1e-9
# manual honored, invalid rejected
assert resolve_glcm_range(sarA, -30.0, 0.0)[2] == "manual"
for bad in [(0.0, -30.0), (5.0, 5.0)]:
    try:
        resolve_glcm_range(sarA, *bad)
        print("Auto-range FAIL"); sys.exit(1)
    except ValueError:
        pass
# auto range rescues previously saturated data: many levels, real contrast
mA = np.zeros((50, 50), bool); mA[10:40, 10:40] = True
cA, _ = extract_candidate_textures(mA, sarA, np.ones((50, 50), bool), "tA",
                                   min_db=lo, max_db=hi)
assert cA[0]["quantized_unique_levels"] > 5
assert cA[0]["texture"]["glcm"]["contrast_mean"] > 0
print(f"Auto-range PASS: [{lo:.2f},{hi:.2f}] levels={cA[0]['quantized_unique_levels']} "
      f"contrast={cA[0]['texture']['glcm']['contrast_mean']:.2f}")

# ---- CSV export contract ----
n = export_csv([str(TMP / "o7" / "s1_spill_meta.json"),
                str(TMP / "o5" / "s1_spill_meta.json")], str(TMP / "b2.csv"))
assert n == 2
with open(TMP / "b2.csv", newline="") as f:
    rows = list(csv.DictReader(f))
assert list(rows[0].keys()) == CSV_HEADER, list(rows[0].keys())
assert rows[0]["wind_speed_kmh"] == "18.5" and rows[0]["distance_to_nearest_vessel_km"] == ""
assert rows[1]["distance_to_nearest_vessel_km"] != "" and rows[1]["wind_speed_kmh"] == ""
assert rows[0]["area_km2"] != "" and rows[0]["label"] == ""  # no labels invented
print("CSV PASS: header exact, missing->empty (never 0), rows=2")
print("ALL B2 TESTS PASSED")
