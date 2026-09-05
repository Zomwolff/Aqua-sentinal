"""Vectorized guard-cell CFAR detection for dark SAR targets."""
from __future__ import annotations

import numpy as np
from scipy.ndimage import uniform_filter


def _validate_window_sizes(guard_size: int, background_size: int) -> None:
    for name, value in (
        ("guard_size", guard_size),
        ("background_size", background_size),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be a positive odd integer.")
        if value <= 0 or value % 2 == 0:
            raise ValueError(f"{name} must be a positive odd integer.")
    if background_size <= guard_size:
        raise ValueError("background_size must be greater than guard_size.")


def cfar_detect(
    image,
    guard_size: int = 3,
    background_size: int = 15,
    k: float = 2.5,
) -> np.ndarray:
    """Detect dark SAR candidates using a guard-cell CFAR annulus.

    ``background_size=15`` denotes the full 15×15 outer window and
    ``guard_size=3`` denotes the full 3×3 inner region excluded from its
    background statistics.

    k=2.5 is an empirical starting value for this project and is not a
    fixed scientific constant. It must be tuned against real Mumbai
    Sentinel-1 scenes. Evaluation should include k=2, k=2.5, and k=3.
    """
    _validate_window_sizes(guard_size, background_size)
    if not np.isfinite(k) or k < 0:
        raise ValueError("k must be a finite non-negative number.")

    source = np.asarray(image)
    if source.size == 0:
        raise ValueError("image must not be empty.")
    if not np.issubdtype(source.dtype, np.number):
        raise ValueError("image must contain numeric values.")

    values = source.astype(np.float64, copy=False)
    finite = np.isfinite(values)
    safe_values = np.where(finite, values, 0.0)

    outer_cells = background_size * background_size
    guard_cells = guard_size * guard_size
    annulus_cells = outer_cells - guard_cells

    outer_mean = uniform_filter(safe_values, size=background_size, mode="reflect")
    guard_mean = uniform_filter(safe_values, size=guard_size, mode="reflect")
    background_mean = (
        outer_mean * outer_cells - guard_mean * guard_cells
    ) / annulus_cells

    outer_mean_sq = uniform_filter(
        safe_values * safe_values,
        size=background_size,
        mode="reflect",
    )
    guard_mean_sq = uniform_filter(
        safe_values * safe_values,
        size=guard_size,
        mode="reflect",
    )
    background_second_moment = (
        outer_mean_sq * outer_cells - guard_mean_sq * guard_cells
    ) / annulus_cells
    background_variance = np.maximum(
        background_second_moment - background_mean * background_mean,
        0.0,
    )
    background_std = np.sqrt(background_variance)

    epsilon = np.finfo(values.dtype).eps
    threshold = background_mean - float(k) * np.maximum(background_std, epsilon)
    return finite & np.isfinite(threshold) & (values < threshold)
