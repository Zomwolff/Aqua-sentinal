# SAR Oil-Spill Pipeline — LinkNet + ResNet34 (B1) + B2 Look-Alike Classifier

End-to-end oil-spill detection from Sentinel-1 SAR imagery:

```
Sentinel-1 TIFF ──▶ infer_pipeline.py ──▶ LinkNet+ResNet34 ──▶ mask
      │                                                        │
      └──────────▶ geo_postprocess.py ◀────────────────────────┘
                        │  mask cleanup + NoData filter + polygons +
                        │  14 B2 features per candidate (GLCM/shape/edge/context)
                        ▼
              spill_meta.json + GeoJSON + SHP
                        │
                        ▼
              b2_lookalike.py + models/b2_random_forest.joblib
                        ▼
                   OIL / LOOK_ALIKE per candidate
```

**Teammate rule #1:** B1 (segmentation) and B2 (classification) are separate stages. Never feed GLCM into the segmentation model, never use model predictions as ground-truth labels, never invent feature values.

---

## 1. Setup (one time)

Requirements: Python 3.14, the checkpoint file, `pip`.

```powershell
cd sar-LinkNet-ResNet34
python -m venv .venv
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

You need `checkpoints/best_model.pth` (~83 MB, **not in git** — see `.gitignore`). Get it from the team drive/USB and place it at exactly that path. Verify:

```powershell
.\.venv\Scripts\python.exe -c "import torch, rasterio, geopandas, skimage, sklearn; print('deps OK')"
```

> CPU-only machines work (slower). GPU needs the CUDA build pinned in `requirements.txt`. On non-3.14 Python, install manually: `pip install torch segmentation-models-pytorch albumentations pillow numpy rasterio geopandas shapely scikit-image pandas pyproj scikit-learn joblib`.

---

## 2. B1 inference — `infer_pipeline.py` (SAR → mask)

```powershell
# single scene (float/dB GeoTIFF -> use --rescale)
.\.venv\Scripts\python.exe infer_pipeline.py --checkpoint checkpoints/best_model.pth --input path\to\scene.tif --output-dir outputs/demo --threshold 0.5 --rescale

# whole folder
.\.venv\Scripts\python.exe infer_pipeline.py --checkpoint checkpoints/best_model.pth --input path\to\folder --output-dir outputs/demo --rescale
```

Per input it writes `{name}_mask.png` (white = oil — **the** downstream file), `{name}_overlay.png` (red tint, eyeball this), `{name}_stats.json` (oil fraction, threshold, preprocessing log).

| Flag | Default | Notes |
|---|---|---|
| `--threshold` | 0.5 | lower = more oil. **Do not change unless you know why.** |
| `--batch-size` | 4 | lower if OOM |
| `--rescale` | off | **on** for float/dB GeoTIFFs |
| `--overlap` | 32 | tiling overlap px |
| `--cfar-mask` / `--gt-mask` | — | optional comparison / metrics only |

Two rules: (1) no accuracy numbers exist without `--gt-mask` — don't quote IoU on new scenes; (2) a `DOMAIN-SHIFT` warning means non-grayscale input — handled automatically, treat as rough.

---

## 3. Post-processing + B2 features — `geo_postprocess.py` (mask → GIS + features)

```powershell
.\.venv\Scripts\python.exe geo_postprocess.py --mask outputs/demo/scene_mask.png --source-image path\to\scene.tif --output-dir outputs/demo --glcm-band 1 --acquisition-time 2026-08-10T00:00:00Z
```

`--source-image` **must be the exact file used for inference** (dimension guard refuses mismatches). What it does, in order: threshold → morphology cleanup → NoData filtering → connected components → **per-candidate masked GLCM/Haralick** (fixed range, symmetric, distances [1,2], angles 0/45/90/135) → shape/edge features → optional wind/AIS/persistence → polygons → files.

Outputs per scene: `{stem}_mask_clean.png`, `{stem}_spill.geojson` (EPSG:4326, full B2 attributes), `{stem}_spill.shp` (native CRS, short aliases — see `shp_field_map` in JSON), `{stem}_spill_meta.json` (everything).

The 14 B2 features per candidate: `mean/std_backscatter`, `glcm_contrast/homogeneity/energy/correlation` (mean+std), `area_m2`, `perimeter_m` (metric — geodesic for geographic CRS, never degrees), `elongation`, `boundary_irregularity = P²/4πA`, `edge_sharpness` (outside−inside dB), `wind_speed_kmh`, `distance_to_nearest_vessel_km`, `persistence_count` (includes current scene, min 1). Missing context stays **null with a status reason** — never 0.

| Flag | Default | Notes |
|---|---|---|
| `--glcm-band` | 1 | explicit 1-based raster band; never averaged; polarization recorded as `unknown` unless metadata proves it |
| `--glcm-min-db` / `--glcm-max-db` | auto (scene p1/p99) | manual override recorded as `manual` in `texture_config.range_source` |
| `--acquisition-time` | — | ISO timestamp; enables time-gated wind/AIS/persistence matching |
| `--weather-csv` | — | `lat,lon,timestamp,wind_speed_kmh[,source]`; nearest within `--weather-window-h` (3h) |
| `--ais-csv` | — | `lat,lon[,timestamp,mmsi]`; min geodesic distance within `--ais-window-h` (24h) |
| `--persist-scene` (+`--persist-time/--persist-scene-id`) | — | repeatable; IoU ≥ `--persist-iou` (0.1) within `--persist-window-h` (720h) |
| `--manual-bounds` | — | `"min_lon,min_lat,max_lon,max_lat"` when source has no CRS (else pixel-space outputs, clearly tagged) |
| `--no-glcm` | off | skip texture, GIS only |

CRS/area behavior: native-CRS shapefile, WGS84 GeoJSON, area via native metric CRS or local-UTM reprojection (never Web Mercator). QGIS check: polygons must sit on the white blobs of `*_mask_clean.png`.

---

## 4. Batch dataset — `at.py` (50 samples → 50 rows)

```powershell
python at.py --data-root dataset/WaterBench_Fusion_50 --lab dataset/Lab.txt --out-dir outputs/batch --csv outputs/batch/dataset_image_level.csv
```

Runs inference + postprocess per `sample_XXXX/S1.tif` (directory name is the authoritative ID), labels **only** from `Lab.txt`, aggregates candidates → one row (MEAN features, SUM `perimeter_m`, MIN vessel distance, `area_m2/1e6` → MEAN), keeps zero-candidate rows blank, then cross-sample persistence matching. Flags: `--limit N` (dry run), `--skip-pipeline` (re-aggregate), `--no-cross-persistence`. New diagnostic columns (`input_min/max/mean_db`, `largest_candidate_fraction`, `notes`) explain saturated/degenerate rows.

---

## 5. B2 classifier — `train_b2.py` + `b2_lookalike.py`

```powershell
# train (needs the 50x25 image-level CSV; original never modified)
python train_b2.py --input data_raw_dataset_original.csv
# -> data/b2_training_clean.csv, models/b2_random_forest.joblib,
#    models/b2_feature_columns.json, reports/b2_random_forest_report.txt,
#    reports/feature_importance.csv

# classify candidates in any spill_meta.json (post-segmentation only)
python b2_lookalike.py --input outputs/batch/sample_0004/S1_spill_meta.json --output outputs/b2_predictions.json
# -> b2_predictions.json (b2{} per candidate + b2_summary), .csv, _candidates.geojson/.shp
```

`b2_lookalike.py` loads the exact 14-column order from `models/b2_feature_columns.json`, maps `texture.glcm.*_mean`, converts `area_m2→area_km2`, passes NaN through the saved imputer Pipeline (bare models without imputers get `insufficient_features` instead of fake predictions), maps classes via `model.classes_` (1=OIL, 0=LOOK_ALIKE), optional `--oil-threshold` (default 0.5 — a B2 decision knob, not the SAR threshold). `oil_probability` = Random Forest probability, **not physical certainty**. B2 output feeds C1 fusion later — C1 is not implemented here.

> Honest status: the current model trained on n=50 scores ~0.40 test accuracy (18/50 rows are fully imputed blanks). It is a **prototype baseline**, not a detector. See `reports/b2_random_forest_report.txt` §19. Improve it with real detections + weather/AIS per candidate + repeat-pass scenes, then rerun `train_b2.py`.

`b2_export.py` converts any `spill_meta.json` to the B2 CSV schema (missing → empty, never 0).

---

## 6. Tests

```powershell
.\.venv\Scripts\python.exe test_glcm_texture.py   # GLCM/masking/ranges (8+ cases)
.\.venv\Scripts\python.exe test_b2_features.py    # B2 features: valid/tiny/NoData/AISx2/weatherx2/persistencex2/degenerate/constant/insufficient + CSV
```

All must end with `ALL * TESTS PASSED`. (Lookalike-service tests run separately with pytest in CI.)

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `Checkpoint not found` | wrong `--checkpoint` path |
| `ValueError: Source SAR size ... != mask size` | `--source-image` isn't the inference input file |
| `NOT GEOREFERENCED` / `PIXEL_SPACE` files | source lacks CRS → add `--manual-bounds` |
| All GLCM = 0/1/1/1 + clip warnings | input range doesn't fit GLCM window — check `clip_fraction_*`, `sar_input`, `notes`; use `--glcm-band` / explicit `--glcm-min/max-db` |
| `persistence_count` stuck at 1 | correct unless scenes overlap — needs repeat-pass/overlapping tiles |
| Masks all black/white | try `--threshold 0.3/0.7`, toggle `--rescale` |
| `PermissionError` on CSV | close it in Excel, rerun |

## 8. File map

`infer_pipeline.py` (B1) · `model.py`/`sar_dataset.py`/`metrics.py` (frozen arch) · `geo_postprocess.py` (GIS + B2 features) · `at.py` (batch) · `b2_export.py` (CSV) · `train_b2.py` (RF) · `b2_lookalike.py` (B2 inference) · `oil_spill_training.ipynb` (training notebook, reference) · `geo_postprocess_spec.md` (original GIS spec)
