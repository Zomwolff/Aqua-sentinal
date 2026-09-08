"""Verification tests for post-model GLCM texture (spec section 30)."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo_postprocess import (
    extract_candidate_textures,
    _haralick_from_glcm,
    _quantize_sar,
    load_sar_band,
    run_postprocess,
)

import rasterio
from rasterio.transform import from_origin
from rasterio.crs import CRS
from PIL import Image

TMP = Path(tempfile.mkdtemp(prefix="glcm_test_"))
print(f"workdir: {TMP}")
rng = np.random.default_rng(7)


def write_geotiff(path, arr, nodata=None, crs=None, transform=None):
    h, w = arr.shape
    crs = crs or CRS.from_epsg(32643)
    transform = transform or from_origin(500000, 2100000, 10, 10)
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype=arr.dtype, crs=crs, transform=transform, nodata=nodata) as dst:
        dst.write(arr, 1)
    return path


def write_mask(path, mask):
    Image.fromarray(mask.astype(np.uint8) * 255).save(path)
    return path


def sar_scene(h=120, w=140, sea_db=-12.0, noise=2.0):
    return (rng.normal(sea_db, noise, (h, w))).astype(np.float32)


# ---- Test 1: normal candidate -> finite features ----
sar = sar_scene()
sar[40:80, 50:100] = rng.normal(-20.0, 1.5, (40, 50))  # dark oil-like patch
mask = np.zeros((120, 140), bool)
mask[40:80, 50:100] = True
cands, cfg = extract_candidate_textures(mask, sar, np.ones_like(mask, bool), "t1")
t = cands[0]
assert t["texture_status"] == "ok", t
g = t["texture"]["glcm"]
for k in ["contrast_mean", "homogeneity_mean", "energy_mean", "correlation_mean",
          "contrast_std", "homogeneity_std", "energy_std", "correlation_std"]:
    assert np.isfinite(g[k]), (k, g[k])
print(f"Test1 PASS normal: { {k: round(g[k],4) for k in g} } pairs={t['glcm_pair_count']}")

# ---- Test 2: small candidate -> documented status, no crash ----
small_mask = np.zeros((30, 30), bool)
small_mask[10:13, 10:13] = True  # 9 px < 20
cands, _ = extract_candidate_textures(small_mask, rng.normal(-12, 1, (30, 30)).astype(np.float64),
                                      np.ones((30, 30), bool), "t2")
assert cands[0]["texture"] is None and cands[0]["texture_status"] == "insufficient_valid_pixels", cands[0]
print("Test2 PASS small ->", cands[0]["texture_status"])

# ---- Test 3: candidate touching NoData -> nodata excluded ----
sar3 = sar_scene(60, 60)
valid3 = np.ones((60, 60), bool)
valid3[:, 50:] = False  # nodata wedge on the right
sar3[:, 50:] = -9999.0
m3 = np.zeros((60, 60), bool)
m3[20:40, 40:58] = True  # straddles valid/nodata boundary
cands, _ = extract_candidate_textures(m3, sar3, valid3, "t3")
# valid px = rows 20 rows x cols 40..49 = 200
assert cands[0]["valid_pixel_count"] == 200, cands[0]
assert cands[0]["texture_status"] == "ok"
print(f"Test3 PASS nodata: valid={cands[0]['valid_pixel_count']} frac={cands[0]['valid_pixel_fraction']:.2f}")

# ---- Test 4: candidate at image boundary -> bbox clipped, no crash ----
sar4 = sar_scene(50, 50)
m4 = np.zeros((50, 50), bool)
m4[0:12, 0:15] = True  # touches top-left corner
cands, _ = extract_candidate_textures(m4, sar4, np.ones((50, 50), bool), "t4")
assert cands[0]["texture_status"] == "ok" and cands[0]["bbox_xyxy"] == [0, 0, 14, 11], cands[0]
print("Test4 PASS boundary:", cands[0]["bbox_xyxy"])

# ---- Test 5: constant region -> no NaN/Inf ----
sar5 = np.full((40, 40), -18.0)
m5 = np.zeros((40, 40), bool)
m5[5:35, 5:35] = True
cands, _ = extract_candidate_textures(m5, sar5, np.ones((40, 40), bool), "t5")
g5 = cands[0]["texture"]["glcm"]
assert all(np.isfinite(v) for v in g5.values()), g5
assert g5["contrast_mean"] == 0.0 and g5["homogeneity_mean"] == 1.0 and g5["energy_mean"] == 1.0, g5
assert g5["correlation_mean"] == 1.0, g5  # documented zero-variance convention
print(f"Test5 PASS constant: { {k: g5[k] for k in ['contrast_mean','homogeneity_mean','energy_mean','correlation_mean']} }")

# ---- Test 6: multiple candidates -> independent features ----
sar6 = sar_scene(100, 100)
sar6[10:40, 10:40] = rng.normal(-22.0, 1.0, (30, 30))  # smooth dark
sar6[60:90, 60:90] = rng.normal(-10.0, 4.0, (30, 30))  # rough bright
m6 = np.zeros((100, 100), bool)
m6[10:40, 10:40] = True
m6[60:90, 60:90] = True
cands, _ = extract_candidate_textures(m6, sar6, np.ones((100, 100), bool), "t6")
assert len(cands) == 2 and all(c["texture_status"] == "ok" for c in cands)
c1, c2 = cands[0]["texture"]["glcm"], cands[1]["texture"]["glcm"]
assert abs(c1["contrast_mean"] - c2["contrast_mean"]) > 1e-6, (c1, c2)  # truly independent
print(f"Test6 PASS multi: contrast {c1['contrast_mean']:.3f} vs {c2['contrast_mean']:.3f}")

# ---- Test 7: dimension mismatch -> clear error, no silent resize ----
sar7 = sar_scene(64, 64)
tif7 = write_geotiff(TMP / "t7.tif", sar7)
m7 = np.zeros((32, 32), bool)
m7[5:20, 5:20] = True
write_mask(TMP / "t7_mask.png", m7)
try:
    run_postprocess(TMP / "t7_mask.png", tif7, TMP / "out7",
                    min_object_px=1, min_hole_px=0, closing_radius=0)
    print("Test7 FAIL: no error raised")
    sys.exit(1)
except ValueError as e:
    assert "!=" in str(e) and "refusing to resize" in str(e).lower() or "must be the exact" in str(e), e
    print(f"Test7 PASS mismatch -> ValueError: {e}")

# ---- Test 8: empty mask -> pipeline works, empty result ----
sar8 = sar_scene(50, 60)
tif8 = write_geotiff(TMP / "t8.tif", sar8)
write_mask(TMP / "t8_mask.png", np.zeros((50, 60), bool))
res = run_postprocess(TMP / "t8_mask.png", tif8, TMP / "out8",
                      min_object_px=1, min_hole_px=0, closing_radius=0)
meta = json.loads((TMP / "out8" / "t8_spill_meta.json").read_text())
assert res["n_polygons"] == 0 and meta["candidates"] == [], meta
print("Test8 PASS empty mask -> n_polygons=0, candidates=[]")

# ---- Full pipeline integration (georeferenced) ----
sarI = sar_scene()
sarI[40:80, 50:100] = rng.normal(-20.0, 1.5, (40, 50))
tifI = write_geotiff(TMP / "scene.tif", sarI, nodata=-9999.0)
mI = np.zeros((120, 140), bool)
mI[40:80, 50:100] = True
write_mask(TMP / "scene_mask.png", mI)
resI = run_postprocess(TMP / "scene_mask.png", tifI, TMP / "outI",
                       min_object_px=1, min_hole_px=0, closing_radius=0)
metaI = json.loads((TMP / "outI" / "scene_spill_meta.json").read_text())
assert (TMP / "outI" / "scene_mask_clean.png").exists()
assert (TMP / "outI" / "scene_spill.geojson").exists()
assert (TMP / "outI" / "scene_spill.shp").exists()
assert len(metaI["candidates"]) == 1 and metaI["candidates"][0]["texture_status"] == "ok"
assert "texture_config" in metaI and metaI["texture_config"]["levels"] == 32
print("Integration PASS:", json.dumps(metaI["candidates"][0]["texture"], indent=1)[:600])

# ---- Mask-awareness check: background must not leak into GLCM ----
sarB = np.full((40, 40), -25.0)
sarB[15:25, 15:25] = -10.0  # bright square inside dark field
mB = np.zeros((40, 40), bool)
mB[15:25, 15:25] = True  # candidate exactly the bright square (constant inside)
candsB, _ = extract_candidate_textures(mB, sarB, np.ones((40, 40), bool), "tB")
gB = candsB[0]["texture"]["glcm"]
assert gB["contrast_mean"] == 0.0, gB  # inside is constant; any nonzero contrast = background leak
print("Mask-awareness PASS: constant-inside candidate contrast=0.0 (no background leak)")

# ---- NaN/Inf exclusion ----
sarN = sar_scene(40, 40)
sarN[0:5, 0:5] = np.nan
sarN[5:10, 0:5] = np.inf
mN = np.zeros((40, 40), bool)
mN[0:20, 0:20] = True
candsN, _ = extract_candidate_textures(mN, sarN, np.ones((40, 40), bool), "tN")
assert candsN[0]["texture_status"] == "ok" and np.isfinite(candsN[0]["texture"]["glcm"]["contrast_mean"])
print("NaN/Inf PASS")

# ---- Regression: full-range saturation -> degenerate-but-correct texture + clip warning ----
import warnings as _w
sarS = rng.normal(-37.0, 2.0, (40, 40))  # genuinely varying, but entirely below -30 dB
mS = np.zeros((40, 40), bool)
mS[5:35, 5:35] = True
assert float(sarS[mS].std()) > 1.0  # prove the input is NOT constant
with _w.catch_warnings(record=True) as rec:
    _w.simplefilter("always")
    candsS, _ = extract_candidate_textures(mS, sarS, np.ones((40, 40), bool), "tS")
c = candsS[0]
assert c["texture_status"] == "ok", c
assert c["clip_fraction_lower"] > 0.95, c  # saturation is recorded, not hidden
assert c["texture"]["glcm"]["contrast_mean"] == 0.0  # correct math for single-level input
assert any("clip outside" in str(x.message) for x in rec), "saturation warning missing"
print(f"Saturation-regression PASS: raw std={float(sarS[mS].std()):.2f} dB, "
      f"clip_lo={c['clip_fraction_lower']:.3f}, contrast={c['texture']['glcm']['contrast_mean']}")

# ---- Test 1 — explicit band selection reads the requested band ----
two = TMP / "twoband.tif"
b1w = rng.normal(-35.0, 1.0, (40, 40)).astype(np.float32)
b2w = rng.normal(-20.0, 1.0, (40, 40)).astype(np.float32)
with rasterio.open(two, "w", driver="GTiff", height=40, width=40, count=2,
                   dtype="float32", crs=CRS.from_epsg(32643),
                   transform=from_origin(500000, 2100000, 10, 10)) as dst:
    dst.write(b1w, 1)
    dst.write(b2w, 2)
sar1, _, info1 = load_sar_band(two, (40, 40), band=1)
sar2, _, info2 = load_sar_band(two, (40, 40), band=2)
assert np.allclose(sar1, b1w, equal_nan=True) and np.allclose(sar2, b2w, equal_nan=True)
assert info1["sar_band"] == 1 and info2["sar_band"] == 2
assert info1["sar_polarization"] == "unknown"  # never guessed from values
assert abs(info1["sar_mean_db"] - float(b1w.mean())) < 1e-4  # scene QC stats present
print(f"Band-select PASS: band1 mean={sar1.mean():.2f} band2 mean={sar2.mean():.2f} "
      f"polarization={info1['sar_polarization']}")

# ---- Test 2 — invalid band rejected ----
for bad in (0, 3, -1):
    try:
        load_sar_band(two, (40, 40), band=bad)
        print(f"Invalid-band FAIL: band={bad} accepted")
        sys.exit(1)
    except ValueError:
        pass
print("Invalid-band PASS: 0/3/-1 rejected with ValueError")

# ---- Test 3 — custom range changes normalization exactly ----
qv = _quantize_sar(np.array([-37.0, -32.0, -27.0, -22.0]), -30.0, 0.0, 32)
assert qv.tolist() == [0, 0, 3, 8], qv.tolist()  # clipped, then floor mapping
mR = np.zeros((20, 20), bool)
mR[5:15, 5:15] = True
sarR = rng.normal(-22.0, 3.0, (20, 20))
cNarrow, _ = extract_candidate_textures(mR, sarR, np.ones((20, 20), bool), "tR-narrow",
                                        min_db=-30.0, max_db=0.0)
cWide, _ = extract_candidate_textures(mR, sarR, np.ones((20, 20), bool), "tR-wide",
                                      min_db=-40.0, max_db=-10.0)
assert cWide[0]["quantized_unique_levels"] >= cNarrow[0]["quantized_unique_levels"]
print(f"Custom-range PASS: narrow levels={cNarrow[0]['quantized_unique_levels']} "
      f"wide levels={cWide[0]['quantized_unique_levels']}")

# ---- Test 4 — invalid range rejected ----
try:
    extract_candidate_textures(mR, sarR, np.ones((20, 20), bool), "tR-bad",
                               min_db=0.0, max_db=-30.0)
    print("Invalid-range FAIL")
    sys.exit(1)
except ValueError:
    print("Invalid-range PASS: min_db >= max_db rejected")

# ---- Test 6 — genuine variation survives quantization ----
assert cWide[0]["quantized_unique_levels"] > 1
assert cWide[0]["texture"]["glcm"]["contrast_mean"] > 0
print(f"Genuine-variation PASS: levels={cWide[0]['quantized_unique_levels']} "
      f"contrast={cWide[0]['texture']['glcm']['contrast_mean']:.3f}")
print("ALL TESTS PASSED")
