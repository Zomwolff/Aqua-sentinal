"""Dark-region segmentation for SAR spill candidate generation.

STEP 3 candidate mask generation is aligned with the design specification
(``docs/context-files/service-deep-dive-spec.md``, module B1): dark regions are
segmented with Otsu thresholding on the Lee-filtered backscatter image, rather
than relying on CFAR alone.

SAR representation and polarity: the worker feeds GEE Sentinel-1 GRD dB values
through ``app.despeckle.lee_filter``; the filtered image is therefore in dB.
In dB a LOWER value means DARKER backscatter, so dark pixels satisfy
``image < threshold`` (a HIGHER dB value is brighter and is never "dark").

This module computes a boolean dark-region mask only. It applies no physical
oil-area threshold (see ``app.morphology.estimate_min_area_px`` and the
``SAR_MIN_AREA_M2`` configuration) and has no DB/Redis side effects.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from skimage.filters import threshold_otsu

_SUPPORTED_METHODS = ("otsu",)


def dark_region_mask(
    image,
    method: Optional[str] = "otsu",
) -> np.ndarray:
    """Return a boolean mask of pixels considered dark backscatter.

    Parameters
    ----------
    image : np.ndarray
        2-D array of SAR backscatter values in dB (lower = darker).
    method : str
        Thresholding method. Only ``"otsu"`` is supported (matches the design
        spec's Otsu segmentation); anything else raises.

    Returns
    -------
    np.ndarray (bool)
        True where the pixel is darker than the computed threshold. Non-finite
        pixels are always False.

    Notes
    -----
    Otsu separates the (dominant, brighter) backscatter population from the
    darker population by minimising within-class variance. The output is the
    low-backscatter class, which in a dB image is ``image < threshold``.

    Global Otsu is not used directly: in SAR scenes a bright population (ship
    hulls, land, strong clutter) coexists with the dark candidate population,
    and global Otsu then splits the scene into "bright" vs "everything else",
    flagging the entire ocean/water background as dark. This is a documented
    equivalent required by the dB SAR representation: the threshold is computed
    with Otsu over the lower-median subset of backscatter values, i.e. the
    darker half of the population. The median is a robust statistic that
    removes bright outliers while preserving the water-vs-dark boundary, so the
    dark class stays the low-backscatter minority rather than the dominant
    water mass.
    """
    if method not in _SUPPORTED_METHODS:
        raise ValueError(
            f"method must be one of {_SUPPORTED_METHODS}, got {method!r}."
        )
    arr = np.asarray(image)
    if arr.ndim != 2:
        raise ValueError("image must be a 2-D array.")
    if arr.size == 0:
        raise ValueError("image must not be empty.")
    if not np.issubdtype(arr.dtype, np.number) and arr.dtype != bool:
        raise ValueError("image must contain numeric backscatter values.")

    values = arr.astype(np.float64, copy=False)
    finite = np.isfinite(values)
    if not finite.any():
        raise ValueError("image contains no finite backscatter values.")

    lo = values[finite]
    median = float(np.median(lo))
    lower = lo[lo <= median]
    if lower.size < 2:
        raise ValueError("image has too few low-backscatter pixels to threshold.")

    if np.ptp(lower) <= 0:
        # Degenerate case: the darker population is uniform (no variance for
        # Otsu to minimise). Split midway between that uniform dark level and
        # the image median so a perfectly uniform dark class is still flagged.
        threshold = 0.5 * (float(np.max(lower)) + median)
    else:
        threshold = threshold_otsu(lower)
    return finite & (values < float(threshold))