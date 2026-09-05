"""Binary mask cleaning and pixel-area threshold estimation for SAR spill candidates.

STEP 3 entry point. Kept deterministic and free of DB/network side effects so it
can be unit-tested without PostGIS or Redis.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from skimage.morphology import (
    binary_closing,
    binary_opening,
    remove_small_objects,
    square,
)

from shared.spatial.constants import SENTINEL1_PIXEL_SIZE_M


def _validate_mask(mask) -> np.ndarray:
    arr = np.asarray(mask)
    if arr.ndim != 2:
        raise ValueError("mask must be a 2-D array.")
    if arr.size == 0:
        raise ValueError("mask must not be empty.")
    return arr.astype(bool, copy=True)


def _validate_footprint_size(name: str, value) -> None:
    if not isinstance(value, (int, np.integer)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer.")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")


def clean_mask(
    mask,
    open_size: int = 3,
    close_size: int = 5,
    min_area_px: Optional[int] = None,
) -> np.ndarray:
    """Clean a binary spill mask with opening then closing.

    ``binary_opening`` (erode + dilate) suppresses speckle-sized dark points;
    ``binary_closing`` (dilate + erode) bridges fine gaps inside a slick so it
    stays one connected component.

    If ``min_area_px`` is supplied, connected components smaller than that many
    pixels are removed with ``remove_small_objects``. The default is None: no
    area threshold is applied here. The physical minimum spill area is an
    explicit caller argument downstream (see ``estimate_min_area_px``) and is
    deliberately never invented inside this function.

    Returns a boolean NumPy array with the same shape as ``mask``.
    """
    _validate_footprint_size("open_size", open_size)
    _validate_footprint_size("close_size", close_size)
    if min_area_px is not None:
        _validate_footprint_size("min_area_px", min_area_px)

    values = _validate_mask(mask)

    opened = binary_opening(values, footprint=square(int(open_size)))
    closed = binary_closing(opened, footprint=square(int(close_size)))

    if min_area_px is not None:
        # connectivity=2 keeps this consistent with skimage.measure.label
        # connectivity=2 used when polygonizing the cleaned mask.
        closed = remove_small_objects(
            closed,
            min_size=int(min_area_px),
            connectivity=2,
        )

    return closed.astype(bool)


def estimate_min_area_px(
    pixel_size_m=SENTINEL1_PIXEL_SIZE_M,
    min_area_m2: Optional[float] = None,
) -> int:
    """Convert a physical minimum spill area (m²) into a pixel count.

    ``min_area_m2`` MUST be supplied explicitly by the caller. There is no
    default noise threshold: the value must come from validation data and be
    passed in, otherwise an error is raised rather than silently choosing a
    number.

    Approximation: a ground-range pixel covers ``pixel_size_m²`` and the pixel
    count is ``min_area_m2 / pixel_area_m2``. The result is rounded to the
    nearest integer and clamped to at least 1 pixel.
    """
    if min_area_m2 is None:
        raise ValueError(
            "min_area_m2 is required and must be supplied explicitly by the "
            "caller; no default noise-area threshold should ever be invented."
        )
    if not np.isfinite(min_area_m2) or min_area_m2 <= 0:
        raise ValueError("min_area_m2 must be a positive finite number.")
    if not np.isfinite(pixel_size_m) or pixel_size_m <= 0:
        raise ValueError("pixel_size_m must be a positive finite number.")

    pixel_area_m2 = float(pixel_size_m) ** 2
    min_area_px = float(min_area_m2) / pixel_area_m2
    return max(1, int(round(min_area_px)))