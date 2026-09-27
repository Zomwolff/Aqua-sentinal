"""GLCM texture features of raw SAR candidates (Step 5).

Texture MUST be computed from the raw pre-despeckle SAR backscatter values: the
Lee despeckling filter used by SAR Step 3 partially removes local texture
detail, so ``filtered_image`` would understate contrast/homogeneity. The caller
retrieves ``raw_image.npy`` from the scene artifact (see
``shared.artifacts``). Never substitute the filtered image.

Pure and deterministic — no DB/Redis access, no Shapely area.
"""
from __future__ import annotations

from typing import Dict

import numpy as np
from skimage.feature import graycomatrix, graycoprops

GLCM_DISTANCES = [1]
GLCM_ANGLES = [0.0, np.pi / 4, np.pi / 2, 3 * np.pi / 4]

_ZERO_FEATURES = {
    "contrast": 0.0,
    "homogeneity": 1.0,
    "energy": 1.0,
    "asm": 1.0,
    "correlation": 1.0,
    "mean_backscatter": 0.0,
    "std_backscatter": 0.0,
}


def _safe(values) -> float:
    """Deterministic finite float; NaN/inf collapse to 0.0."""
    arr = np.asarray(values, dtype=np.float64)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    return float(arr.mean())


def compute_glcm_features(
    raw_image,
    region_mask,
    levels: int = 32,
) -> Dict[str, float]:
    """Compute GLCM texture + raw backscatter statistics for a candidate.

    Parameters
    ----------
    raw_image : np.ndarray
        Pre-despeckle SAR backscatter values (dB). Never the Lee-filtered image.
    region_mask : np.ndarray (bool)
        Candidate pixels, same shape as ``raw_image``.
    levels : int
        Gray-level quantization (>= 2).

    Returns
    -------
    dict with keys:
        contrast, homogeneity, energy, asm, correlation
        (mean over 4 GLCM angles; energy is sqrt(ASM), both reported)
        mean_backscatter, std_backscatter           (raw dB values in the mask)

    B2 GLCM SOURCE NOTE: this service uses per-candidate min/max normalization
    and a single distance. Older B2 reports used a fixed [-30, 0] dB range,
    distances [1, 2], and mean+std aggregation. Do not mix those features
    with this service's features without accounting for the normalization.

    Empty or fully-invalid regions return the documented zero/neutral features;
    a uniform region (no gray-level variation) returns a homogeneous-feature
    response (contrast 0) rather than crashing.
    """
    raw = np.asarray(raw_image, dtype=np.float64)
    mask = np.asarray(region_mask, dtype=bool)
    if raw.ndim != 2 or mask.ndim != 2:
        raise ValueError("raw_image and region_mask must be 2-D arrays.")
    if raw.shape != mask.shape:
        raise ValueError("raw_image and region_mask must share the same shape.")
    if int(levels) < 2:
        raise ValueError("levels must be an integer >= 2.")

    masked_values = raw[mask]
    if masked_values.size == 0:
        return dict(_ZERO_FEATURES)

    finite = np.isfinite(masked_values)
    if not finite.any():
        return dict(_ZERO_FEATURES)
    values = masked_values[finite]

    mean_backscatter = float(np.mean(values))
    std_backscatter = float(np.std(values))

    vmin = float(np.min(values))
    vmax = float(np.max(values))
    if vmax <= vmin:
        # No intensity variation: nothing for a GLCM to describe. Report the
        # neutral uniform-region features plus the raw statistics.
        return {
            "contrast": 0.0,
            "homogeneity": 1.0,
            "energy": 1.0,
            "asm": 1.0,
            "correlation": 1.0,
            "mean_backscatter": mean_backscatter,
            "std_backscatter": std_backscatter,
        }

    n_levels = int(levels)
    quantized = np.zeros(raw.shape, dtype=np.int64)
    scaled = (values - vmin) / (vmax - vmin) * (n_levels - 1)
    quantized[mask] = np.clip(scaled, 0, n_levels - 1)
    # Sentinel level isolates candidate pairs: pairs that touch a non-candidate
    # pixel land in the sentinel row/column and are dropped after counting.
    sentinel = n_levels
    quantized[~mask] = sentinel

    matrix = graycomatrix(
        quantized,
        distances=GLCM_DISTANCES,
        angles=GLCM_ANGLES,
        levels=n_levels + 1,
        symmetric=False,
        normed=False,
    )
    # Drop the sentinel row/column, symmetrise and normalise — reproducing the
    # symmetric=True / normed=True semantics over candidate-only pixel pairs.
    matrix = matrix[:n_levels, :n_levels]
    matrix = matrix + matrix.transpose(1, 0, 2, 3)
    total = float(matrix.sum())
    if total <= 0:
        return {
            "contrast": 0.0,
            "homogeneity": 1.0,
            "energy": 1.0,
            "asm": 1.0,
            "correlation": 1.0,
            "mean_backscatter": mean_backscatter,
            "std_backscatter": std_backscatter,
        }
    normalized = matrix / total

    # energy is sqrt(ASM) by the Haralick definition; ASM is preserved
    # separately because existing consumers (scoring uniform_term, dashboard
    # displays) historically read the "energy" key. Both endpoints coincide
    # (0/1), so heuristic thresholds keep their qualitative meaning.
    asm = _safe(graycoprops(normalized, "ASM"))
    return {
        "contrast": _safe(graycoprops(normalized, "contrast")),
        "homogeneity": _safe(graycoprops(normalized, "homogeneity")),
        "energy": float(np.sqrt(max(asm, 0.0))),
        "asm": asm,
        "correlation": _safe(graycoprops(normalized, "correlation")),
        "mean_backscatter": mean_backscatter,
        "std_backscatter": std_backscatter,
    }
