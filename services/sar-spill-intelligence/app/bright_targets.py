"""Bright-target vectorization for SAR scenes.

Converts the binary bright_target_mask produced during Step 3 into vector
object records (centroid lon/lat, extent-based length estimate) using the
scene's affine transform. These vectors are published to the ``sar.objects``
Redis stream and consumed by the dark-vessel detector's SAR-correlation
mechanism — previously that mechanism had no coordinates to work from.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

import numpy as np
from scipy.ndimage import label

# Minimum connected-component size (pixels) for a bright target to count as a
# possible vessel: at 10 m Sentinel-1 IW pixels even a 25 m boat spans ~2-3 px,
# but single-pixel speckle residuals must not survive despeckling+CFAR.
MIN_TARGET_PIXELS = int(3)


def _apply_affine(transform: Sequence[float], col: float, row: float) -> tuple:
    """Map pixel (row, col) through an GDAL-style 6-element affine to (lon, lat)."""
    a, b, c, d, e, f = (float(x) for x in transform[:6])
    # x = a*col + b*row + c ; y = d*col + e*row + f
    return a * col + b * row + c, d * col + e * row + f


def extract_bright_targets(
    bright_mask: np.ndarray,
    transform: Sequence[float],
    pixel_size_m: float = 10.0,
    min_pixels: int = MIN_TARGET_PIXELS,
) -> List[Dict[str, Any]]:
    """Vectorize connected components of ``bright_mask``.

    Returns one dict per target:
      lat, lon          — component centroid in EPSG:4326 degrees
      pixel_count       — number of pixels in the component
      length_est_m      — max(row_extent, col_extent) * pixel_size_m
    """
    source = np.asarray(bright_mask)
    if source.ndim != 2:
        raise ValueError("bright_mask must be a 2-D array.")
    if source.size == 0:
        return []

    labels, count = label(source.astype(bool))
    if count == 0:
        return []

    targets: List[Dict[str, Any]] = []
    rows_idx, cols_idx = np.indices(labels.shape)
    for lab in range(1, count + 1):
        sel = labels == lab
        pixel_count = int(sel.sum())
        if pixel_count < min_pixels:
            continue
        row_c = float(rows_idx[sel].mean())
        col_c = float(cols_idx[sel].mean())
        row_span = int(rows_idx[sel].max() - rows_idx[sel].min()) + 1
        col_span = int(cols_idx[sel].max() - cols_idx[sel].min()) + 1
        lon, lat = _apply_affine(transform, col_c, row_c)
        targets.append(
            {
                "lat": float(lat),
                "lon": float(lon),
                "pixel_count": pixel_count,
                "length_est_m": float(max(row_span, col_span) * pixel_size_m),
            }
        )

    targets.sort(key=lambda t: (-t["pixel_count"], t["lat"], t["lon"]))
    return targets
