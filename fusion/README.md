# Production Fusion API

The integration entrypoint is:

```python
from fusion import detect_oil

mask = detect_oil("S1.tif", "S2.tif")
# uint8 NumPy array, values 0 (not oil) or 1 (oil)
```

All three modes are supported:

```python
sar_mask = detect_oil(sentinel1_path="S1.tif")
eo_mask = detect_oil(sentinel2_path="S2.tif")
fused_mask = detect_oil(sentinel1_path="S1.tif", sentinel2_path="S2.tif")
```

For a clear scene-level decision, use `detect_oil_result(...)`. It reports `oil_spill_detected` based on whether the returned mask contains at least one detected pixel. The CLI prints `OIL_SPILL_DETECTED` or `NO_OIL_SPILL`.

`sentinel2_path` may be a 10-band GeoTIFF with canonical band descriptions or a directory containing separate files whose names contain `B02`, `B03`, `B04`, `B05`, `B06`, `B07`, `B08`, `B8A`, `B11`, and `B12`. Separate bands are detected by filename, ordered canonically, and resampled to the B2 grid when required.

The package uses the supplied SAR UNet-ResNet34 checkpoint exported to ONNX and the existing binary EO ONNX network. It preserves the reference preprocessing, tiling, stitching, band order, and normalization constants. It aligns SAR probability to the EO grid. Standalone SAR uses threshold `0.50`; standalone EO uses threshold `0.40`. For fused inputs, the continuous probabilities are compared on the common EO grid and thresholded at `0.50` after weighted fusion. Both EO upload formats use 50% SAR / 50% EO. Fusion combines continuous oil probabilities before thresholding; rendered images and per-model binary masks are never fusion inputs. Only pixels valid in both aligned sources participate.

```text
FUSED_PROBABILITY = SAR_WEIGHT * SAR_probability + EO_WEIGHT * EO_probability
FINAL_MASK = FUSED_PROBABILITY >= 0.50
```

The `detect_oil` API returns only a mask; `run_fusion` adds persisted metadata and measured candidate outputs. `detect_oil_result` provides the explicit mode and scene-level boolean when needed.

## Local validation

From the repository root:

```powershell
python -c "from fusion import detect_oil; import numpy as np; m=detect_oil('S1.tif','S2.tif'); print(m.shape, m.dtype, np.unique(m))"
```

## ONNX

The production image contains only `models/sar/model.onnx`, `models/eo/model.onnx`, `models/sar/normalization.json`, SAR provenance, and `models/eo/normalization.json`. Existing Python preprocessing, tiling, overlap stitching, sigmoid, thresholds, and raster alignment remain outside the graphs. Model export and PyTorch-vs-ONNX validation are development tasks and are not dependencies of the production package.

## Docker

Build from the repository root so the Dockerfile can copy the canonical checkpoints:

```powershell
docker build -f fusion/Dockerfile -t aqua-sentinel-fusion .
docker run --rm -v "D:\data:/data" aqua-sentinel-fusion /data/S1.tif /data/S2.tif
```

## Integrated upload output

`run_fusion(..., output_dir=..., acquisition_time=None, scene_id=None, ais_time=None)` writes `fusion_metadata.json`. Acquisition time is explicit first, then TIFF `ACQUISITION_TIME`/`TIFFTAG_DATETIME`, then saved AIS time (labelled `ais_fallback`). Missing time requires explicit input; processing time is never presented as satellite acquisition time.

Each run also writes `oil_spill_visualization.png`. It preserves the source raster's measured luminance and texture, applies a restrained ocean palette, and darkens only the final-mask footprint for operator review. The dashboard labels this artifact as an illustrative, mask-guided colourisation; it is not presented as measured true-colour imagery or as additional model evidence.

Scene ID comes from the source filename or explicit parameter. Candidate outputs include measured `area_m2`, `area_km2`, and mean model probability. Only Sentinel-1 supplies exported georeferencing. Fused masks are exported on its grid; EO-only results remain optical evidence with no mapped polygons or invented area. Existing EO coordinates may be read for alignment, but no coordinates are assigned to the EO input.

Each mapped Sentinel-1 candidate also stores measured shape descriptors, GLCM
texture, boundary evidence, the ONNX model version, and proximity to saved AIS
positions within 20 km and six hours. The gateway exposes the record at
`GET /spill/candidates/{candidate_id}` and includes it in incident details for
the dashboard. Run `python -m fusion.backfill_candidate_evidence` once inside
the fusion service to populate those fields for uploads created by older builds.

`POST /fusion/upload` through the frontend accepts S1, S2, or both, plus `mmsi`, optional `acquisition_time` and `scene_id`. Results are saved and candidates pass through Redis `spill.candidates.filtered` → `incident.fused` → `spill.attributed`. Attribution mints a stable UUID separate from `candidate_id`. Apply `infra/postgres/migrations/004_fusion_candidate_link.sql` to existing databases before starting the updated backend.

The active UI is `frontend/`, served by Docker at http://localhost:3000. The `dashboard/` directory is historical. The Vite development proxy uses the same `/api`, `/fusion`, `/artifacts`, and `/live` paths.

### Verification (2026-09-10)

- The supplied S1.tif ran through the frontend upload route using the actual SAR ONNX model: 10,609 positive pixels, 119 connected candidate regions, total geodesic area 1.0637057818660065 km². Small positive regions are retained rather than silently discarded. These are model detections, not independently confirmed spills.
- Acquisition time was taken from the selected vessel's saved AIS timestamp because this TIFF has no acquisition tag; metadata identifies `ais_fallback`.
- Attribution produced separate spill IDs; five forecast horizons, recommendations, and response-cost estimates were persisted.
- Ten metadata/attribution regressions passed, including area conversion and S1-only exported georeferencing. Both ONNX sessions load. A real S2 image was not supplied for end-to-end EO validation.
- Existing volumes also need `infra/postgres/migrations/003_response_decision.sql`. `scripts/backfill_response_costs.py` repairs only missing costs in the backend container, preserving existing recommendations.
- The old standalone `eo` service is opt-in under the `legacy-eo` Compose profile. Local S2 uploads use `fusion`.

## SAR input contract

The SAR checkpoint is from [sar-UNET-RESNET34](https://github.com/aditya-mensinkai/sar-UNET-RESNET34), commit `e4bda07ccc8517b4c47fd2d60dfc137d7e653ca8`. The supplied `best_model.pth.zip` is itself a PyTorch checkpoint archive. Its SHA-256 and ONNX parity results are recorded in `models/sar/provenance.json`.

Supply exactly two calibrated Sigma0-dB bands in the original training file order. The upstream project explicitly leaves the VV/VH assignment unresolved. We preserve file order, do not guess the polarization mapping, and reject single-band/RGB inputs. The existing automatic VV-only acquisition feed is incompatible until the training band order is established and acquisition supplies both channels in that order.

Inference applies dB-to-linear conversion, a 5x5 Lee filter, linear-to-dB conversion, and frozen per-band normalization. It uses 512x512 windows with stride 384, reflect padding for small images, sigmoid once, and overlap-mean probability stitching. The numerical Lee implementation in `fusion/sar_filter.py` comes from the pinned reference repository. No training, augmentation, optimizer, or dataset code is included in production. PyTorch and ONNX export tools were used only in a temporary environment outside this repository.

The attached `unetBinary.py` is a training reference, not an EO checkpoint. The existing EO ONNX weights and normalization are retained; the EO decision threshold is fixed at 0.40.
