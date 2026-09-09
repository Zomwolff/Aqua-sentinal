# Production Fusion API

The integration entrypoint is:

```python
from fusion import detect_oil

mask = detect_oil("S1.tif", "S2.tif")
# uint8 NumPy array, values 0 (not oil) or 1 (oil)
```

`sentinel2_path` may be a 10-band GeoTIFF with canonical band descriptions or a directory containing separate files whose names contain `B02`, `B03`, `B04`, `B05`, `B06`, `B07`, `B08`, `B8A`, `B11`, and `B12`. Separate bands are detected by filename, ordered canonically, and resampled to the B2 grid when required.

The package uses ONNX exports of the existing SAR and EO networks. It preserves the reference preprocessing, tiling, stitching, band order, and normalization constants. It aligns SAR probability to the EO grid. The production thresholds are SAR `0.50` and EO `0.15`. The final mask is EO-primary:

```text
EO_MASK = EO_probability >= 0.15
SAR_MASK = SAR_probability >= 0.50
FINAL_MASK = EO_MASK
```

SAR agreement is internal supporting evidence; SAR-only detections are rejected and SAR does not erase EO detections. The public API returns no probabilities, metadata, or intermediate masks.

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
