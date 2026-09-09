# Sentinel-2 Interface

`optical imagery/sentinel2_interface.py` loads local Sentinel-2 imagery and
runs the trained MADOS U-Net segmentation model. It does not download imagery
or acquire remote Sentinel-2 scenes.

The model expects these ten bands in this order:

`B2`, `B3`, `B4`, `B5`, `B6`, `B7`, `B8`, `B8A`, `B11`, `B12`

The Oil Spill class is MADOS class `6`.

## Supported inputs

The interface accepts:

1. A single MADOS B2 GeoTIFF patch.
2. A directory containing exactly one MADOS patch.
3. A ten-band GeoTIFF whose bands are already in the order listed above.

MADOS band files must follow the standard naming convention, for example:

```text
Scene_104_L2R_rhorc_492_11.tif
Scene_104_L2R_rhorc_559_11.tif
```

When a B2 patch is provided, the interface locates the remaining bands in the
scene's `10` and `20` directories. The 20 m bands are resampled to the B2
dimensions using nearest-neighbor resampling.

## Python API

```python
from sentinel2_interface import get_data, predict

data = get_data("path/to/mados_b2_patch.tif")
result = predict(data)

oil_probability = result["oil_probability"]
predicted_classes = result["predicted_classes"]
oil_mask = result["oil_mask_argmax"]
```

`get_data()` also accepts a ten-band GeoTIFF:

```python
data = get_data("scene_10band.tif")
```

Optional location arguments are validated but do not download or select
imagery:

```python
data = get_data(
      "scene_10band.tif",
      latitude=19.2,
      longitude=72.8,
      bbox=(72.0, 18.5, 73.0, 19.5),
)
```

`get_fusion_output()` remains available for compatibility with older callers.
Its `center` argument is ignored because the U-Net performs full-tile
segmentation rather than center-window classification.

## Model preparation

The model always receives a 256 x 256 tile.

- Smaller inputs are zero-padded on the bottom and right.
- Larger inputs are center-cropped.
- Outputs are cropped back to the original dimensions for smaller inputs.
- Center-cropping preserves the corresponding geospatial transform.
- Input values are normalized using the mean and standard deviation stored in
  the model checkpoint.

## Prediction result

`predict()` returns a dictionary containing:

| Key                   | Description                                                       |
| --------------------- | ----------------------------------------------------------------- |
| `oil_probability`     | Continuous Oil Spill probability map with shape `H x W`.          |
| `class_probabilities` | Softmax probabilities for all 15 classes with shape `15 x H x W`. |
| `predicted_classes`   | Argmax MADOS class IDs from 1 through 15.                         |
| `oil_mask_argmax`     | Binary mask where Oil Spill is the argmax class.                  |
| `source_shape`        | Original source image dimensions.                                 |
| `prepared_shape`      | Dimensions used for model inference.                              |
| `prep_info`           | Padding and crop metadata.                                        |
| `transform`           | Geospatial transform for the prediction output.                   |
| `crs`                 | Source coordinate reference system.                               |
| `profile`             | Source raster profile.                                            |
| `device`              | Device used by PyTorch, such as `cpu` or `cuda`.                  |

## Command-line usage

Run the interface from the repository root:

```powershell
python "optical imagery/sentinel2_interface.py" `
   "D:/path/to/B2_patch.tif"
```

Run inference on a ten-band GeoTIFF:

```powershell
python "optical imagery/sentinel2_interface.py" `
   "D:/path/to/10_band.tif" `
   --checkpoint "optical imagery/sentinel2_unet_seg_best.pth" `
   --output-dir "inference_output"
```

Available options:

```text
--checkpoint PATH
--output-dir PATH
--threshold FLOAT
--device cpu|cuda
```

`--threshold` must be between `0` and `1`. It controls the additional
threshold-based binary mask and defaults to `0.5`. The normal multiclass oil
mask is produced using argmax.

## Output files

The command-line interface writes these files to the output directory:

| File                     | Description                                           |
| ------------------------ | ----------------------------------------------------- |
| `oil_probability.tif`    | Continuous Oil Spill probability GeoTIFF.             |
| `oil_probability.npy`    | NumPy version of the probability map.                 |
| `predicted_classes.tif`  | Predicted MADOS class IDs.                            |
| `oil_mask_argmax.tif`    | Binary mask from multiclass argmax.                   |
| `oil_mask_threshold.tif` | Binary mask using the selected probability threshold. |

The GeoTIFF outputs preserve the source raster's geospatial metadata,
coordinate reference system, and transform where applicable.

## Checkpoint and dependencies

By default, the interface loads:

```text
optical imagery/sentinel2_unet_seg_best.pth
```

Install the optical-imagery dependencies with:

```powershell
pip install -r "optical imagery/requirements.txt"
```

The interface requires Python packages including NumPy, Rasterio, and PyTorch.
A valid U-Net checkpoint must contain:

- `model_state_dict`
- `feature_names`
- `num_bands`
- `num_classes`
- `fixed_size`
- `normalization_mean`
- `normalization_std`
