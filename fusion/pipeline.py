from pathlib import Path
import tempfile

import numpy as np
import rasterio

from .alignment import align_sar_probability
from .config import EO_ONNX, EO_THRESHOLD, SAR_ONNX, SAR_THRESHOLD


def detect_oil(sentinel1_path, sentinel2_path) -> np.ndarray:
    """Return the EO-primary final oil mask as uint8 values {0, 1}."""
    if SAR_ONNX.is_file() and EO_ONNX.is_file():
        from .onnx_runtime import detect_oil_onnx
        mask, _, _ = detect_oil_onnx(sentinel1_path, sentinel2_path, SAR_ONNX, EO_ONNX)
        return mask
    if not SAR_ONNX.is_file() or not EO_ONNX.is_file():
        raise FileNotFoundError("Production ONNX model files are required in models/sar and models/eo")
    from .onnx_runtime import detect_oil_onnx
    mask, _, _ = detect_oil_onnx(sentinel1_path, sentinel2_path, SAR_ONNX, EO_ONNX)
    return mask.astype(np.uint8, copy=False)


def save_mask(mask: np.ndarray, reference_path, output_path) -> None:
    """Optional helper for callers that need a georeferenced GeoTIFF."""
    if mask.dtype != np.uint8 or not np.isin(mask, [0, 1]).all():
        raise ValueError("mask must be uint8 with values only 0 and 1")
    with rasterio.open(reference_path) as reference:
        profile = reference.profile.copy()
        profile.update(count=1, dtype="uint8", nodata=0, compress="deflate")
        with rasterio.open(output_path, "w", **profile) as destination:
            destination.write(mask, 1)
            destination.write_mask(reference.read_masks(1))
