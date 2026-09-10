# Manual Fusion Upload API

This package is the ad-hoc upload-triggered path exposed by the standalone
`fusion` container on port 8017 (`/fusion/upload`). It is separate from
`services/evidence-fusion`, the automated Redis-stream worker inside the
`backend` container. The manual API fuses user-supplied rasters immediately;
the worker correlates persisted SAR candidates with AIS risk and optical
evidence.

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

The package uses ONNX exports of the existing SAR and EO networks. It preserves the reference preprocessing, tiling, stitching, band order, and normalization constants. It aligns SAR probability to the EO grid. Standalone SAR uses threshold `0.50`; standalone EO uses threshold `0.15`. For fused inputs, the continuous probabilities are compared on the common EO grid and thresholded at `0.50` after weighted fusion. A single multiband EO GeoTIFF uses 75% SAR / 25% EO; a separate-band EO directory uses 50% / 50%.

```text
FUSED_PROBABILITY = SAR_WEIGHT * SAR_probability + EO_WEIGHT * EO_probability
FINAL_MASK = FUSED_PROBABILITY >= 0.50
```

The public API returns no probabilities, metadata, or intermediate masks. `detect_oil_result` provides the explicit mode and scene-level boolean when needed.

## Local validation

From the repository root:

```powershell
python -c "from fusion import detect_oil; import numpy as np; m=detect_oil('S1.tif','S2.tif'); print(m.shape, m.dtype, np.unique(m))"
```

## ONNX

The production image contains only `models/sar/model.onnx`, `models/eo/model.onnx`, and `models/eo/normalization.json`. Existing Python preprocessing, tiling, overlap stitching, sigmoid, thresholds, and raster alignment remain outside the graphs. Model export and PyTorch-vs-ONNX validation are development tasks and are not dependencies of the production package.

## Docker

Build from the repository root so the Dockerfile can copy the canonical checkpoints:

```powershell
docker build -f fusion/Dockerfile -t aqua-sentinel-fusion .
docker run --rm -v "D:\data:/data" aqua-sentinel-fusion /data/S1.tif /data/S2.tif
```
