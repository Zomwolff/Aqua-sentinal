"""Vectorized Lee-style SAR despeckling."""
from __future__ import annotations

import numpy as np
from scipy.ndimage import uniform_filter


def _validate_window_size(window_size: int) -> None:
    if not isinstance(window_size, int) or isinstance(window_size, bool):
        raise ValueError("window_size must be a positive odd integer.")
    if window_size <= 0 or window_size % 2 == 0:
        raise ValueError("window_size must be a positive odd integer.")


def lee_filter(image: np.ndarray, window_size: int = 5) -> np.ndarray:
    """Apply a vectorized Lee-style local-statistics speckle filter.

    The input is preserved in its supplied numeric convention. STEP 1 exports
    GEE Sentinel-1 GRD dB values, so this implementation deliberately applies
    the local-statistics approximation in dB and does not silently convert to
    linear power. A future physically calibrated linear-power Lee variant must
    make that conversion explicit at its call site.

    5×5 provides less smoothing and retains more spatial detail than 7×7,
    which is preferable for preserving potential oil-spill boundaries at
    this stage; 7×7 would provide stronger smoothing but risks removing
    small spill structures.
    """
    _validate_window_size(window_size)

    source = np.asarray(image)
    if source.size == 0:
        raise ValueError("image must not be empty.")
    if not np.issubdtype(source.dtype, np.number):
        raise ValueError("image must contain numeric values.")
    if not np.all(np.isfinite(source)):
        raise ValueError("image must contain only finite values.")

    values = source.astype(np.float64, copy=True)
    local_mean = uniform_filter(values, size=window_size, mode="reflect")
    local_mean_sq = uniform_filter(
        values * values,
        size=window_size,
        mode="reflect",
    )
    local_variance = np.maximum(local_mean_sq - local_mean * local_mean, 0.0)
    noise_variance = float(np.mean(local_variance))
    epsilon = np.finfo(values.dtype).eps
    weights = local_variance / (local_variance + noise_variance + epsilon)

    return local_mean + weights * (values - local_mean)
