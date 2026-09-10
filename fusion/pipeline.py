from pathlib import Path

import numpy as np
import rasterio

from .config import EO_ONNX, EO_THRESHOLD, SAR_ONNX, SAR_THRESHOLD


def _require_models(*paths):
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing production ONNX model file(s): {missing}")


def _detect_oil_result(sentinel1_path=None, sentinel2_path=None) -> dict:
    """Return the mask plus a concise oil/no-oil decision and mode metadata."""
    if sentinel1_path is None and sentinel2_path is None:
        raise ValueError("Provide sentinel1_path, sentinel2_path, or both")
    from .onnx_runtime import predict_sar_probability, predict_eo_probability, align_probability_pair

    if sentinel1_path is not None and sentinel2_path is None:
        _require_models(SAR_ONNX)
        probability, valid = predict_sar_probability(sentinel1_path, SAR_ONNX)
        mask = ((probability >= SAR_THRESHOLD) & valid).astype(np.uint8)
        return {"mask": mask, "probability": probability, "oil_spill_detected": bool(mask.any()), "mode": "SAR_ONLY", "threshold": SAR_THRESHOLD, "weights": None}

    if sentinel1_path is None and sentinel2_path is not None:
        _require_models(EO_ONNX)
        probability, valid = predict_eo_probability(sentinel2_path, EO_ONNX)
        mask = ((probability >= EO_THRESHOLD) & valid).astype(np.uint8)
        return {"mask": mask, "probability": probability, "oil_spill_detected": bool(mask.any()), "mode": "EO_ONLY", "threshold": EO_THRESHOLD, "weights": None}

    _require_models(SAR_ONNX, EO_ONNX)
    sar_probability, sar_valid = predict_sar_probability(sentinel1_path, SAR_ONNX)
    eo_probability, eo_valid = predict_eo_probability(sentinel2_path, EO_ONNX)
    sar_aligned, valid = align_probability_pair(sentinel1_path, sar_probability, sar_valid, sentinel2_path, eo_valid)
    sar_weight = 0.75 if Path(sentinel2_path).is_file() else 0.50
    eo_weight = 1.0 - sar_weight
    fused_probability = sar_weight * sar_aligned + eo_weight * eo_probability
    mask = ((fused_probability >= 0.50) & valid).astype(np.uint8)
    sar_mask = (sar_aligned >= SAR_THRESHOLD) & valid
    eo_mask = (eo_probability >= EO_THRESHOLD) & valid
    return {
        "mask": mask,
        "probability": fused_probability,
        "oil_spill_detected": bool(mask.any()),
        "mode": "FUSED",
        "threshold": 0.50,
        "weights": {"sar": sar_weight, "eo": eo_weight},
        "fused_pixel_count": int(mask.sum()),
        "sar_supported_pixel_count": int((mask.astype(bool) & sar_mask).sum()),
        "eo_only_pixel_count": int((mask.astype(bool) & ~sar_mask).sum()),
        "sar_only_rejected_pixel_count": int((sar_mask & ~eo_mask).sum()),
    }


def detect_oil_result(sentinel1_path=None, sentinel2_path=None) -> dict:
    result = _detect_oil_result(sentinel1_path, sentinel2_path)
    result.pop("probability")
    return result


def detect_oil(sentinel1_path=None, sentinel2_path=None) -> np.ndarray:
    """Return only a uint8 binary oil mask. Provide S1, S2, or both."""
    return detect_oil_result(sentinel1_path, sentinel2_path)["mask"]


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
