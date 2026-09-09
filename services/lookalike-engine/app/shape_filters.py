"""Heuristic lookalike rejection: discriminating non-spill SAR regions.

STEP 4 shape filtering for the SAR spill intelligence pipeline.

These classifiers are heuristics for REJECTING lookalikes (ship shadows,
calm-water patches). They never claim ground truth or confirmed oil detection.
The strongest positive statement any candidate can receive is ``possible_slick``:

    possible_slick == "this candidate was NOT rejected by the implemented
                      lookalike heuristics"

It does NOT mean "confirmed oil spill".

Allowed classifications (never anything else):
    likely_ship_shadow
    likely_calm_water
    possible_slick

These functions are pure and deterministic: no DB, no Redis, no network.
All pixel-level inputs are supplied by the caller; nothing is fabricated or
reconstructed here.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
from skimage import measure
from skimage.morphology import dilation, disk

ALLOWED_LABELS = ("likely_ship_shadow", "likely_calm_water", "possible_slick")

# Elongation (major/minor axis ratio) is unbounded for a 1-px-wide line; cap it
# so the descriptor stays finite and JSON-serialisable.
ELONGATION_CAP = 1e6

_EPS = np.finfo(np.float64).eps


def _as_bool2d(name: str, array) -> np.ndarray:
    arr = np.asarray(array)
    if arr.ndim != 2:
        raise ValueError(f"{name} must be a 2-D array.")
    if arr.size == 0:
        raise ValueError(f"{name} must not be empty.")
    return arr.astype(bool)


def _largest_region(label_image):
    props = measure.regionprops(label_image)
    if not props:
        return None
    return max(props, key=lambda p: p.area)


def compute_shape_descriptors(candidate: Dict[str, Any], mask_region) -> Dict[str, float]:
    """Compute shape descriptors for a candidate from its dark-mask patch.

    Parameters
    ----------
    candidate : dict
        Candidate record (used for context; e.g. ``pixel_count``). The polygon
        geometry must already be in (longitude, latitude) order — this function
        never touches or reorders coordinates.
    mask_region : np.ndarray
        2-D boolean array of the candidate's dark pixels, aligned with the
        bright-target mask used elsewhere.

    Returns
    -------
    dict with at least:
        eccentricity        regionprops.eccentricity
        elongation          major_axis_length / minor_axis_length
                            (capped at ELONGATION_CAP for zero minor axis)
        solidity            regionprops.solidity
        perimeter_area_ratio perimeter / area (0 when area == 0)
        area_px             pixel count of the region

    Derived from ``skimage.measure.regionprops`` pixel measurements — never
    Shapely planar (degree-based) area.
    """
    mask = _as_bool2d("mask_region", mask_region)
    labels = measure.label(mask, connectivity=2)
    region = _largest_region(labels)

    empty = {
        "eccentricity": 0.0,
        "elongation": 1.0,
        "solidity": 0.0,
        "perimeter_area_ratio": 0.0,
        "area_px": 0,
    }
    if region is None:
        return empty

    area = float(region.area)
    minor = float(region.axis_minor_length)
    major = float(region.axis_major_length)
    if minor <= _EPS:
        elongation = float(ELONGATION_CAP)
    else:
        elongation = major / minor

    return {
        "eccentricity": float(region.eccentricity) if area > 0 else 0.0,
        "elongation": float(elongation),
        "solidity": float(region.solidity) if area > 0 else 0.0,
        "perimeter_area_ratio": (
            float(region.perimeter) / area if area > 0 else 0.0
        ),
        "area_px": int(area),
    }


def _edge_gradient(intensity: np.ndarray, dark_mask: np.ndarray) -> float:
    """Ratio of mean backscatter outside the dark region to inside the dark region.

    Values near 1.0 mean a diffuse boundary (wind shadow/calm water). High values
    indicate a sharp drop in backscatter typical of true oil slicks. Returns
    0.0 when there is no usable data.
    """
    from skimage.morphology import dilation, erosion, disk

    dark = np.asarray(dark_mask, dtype=bool)
    int_arr = np.asarray(intensity, dtype=np.float64)

    dilated = dilation(dark, footprint=disk(3))
    eroded = erosion(dark, footprint=disk(3))

    boundary_outer = dilated & ~dark
    boundary_inner = dark & ~eroded

    val_outer = int_arr[boundary_outer]
    val_inner = int_arr[boundary_inner]

    val_outer_finite = val_outer[np.isfinite(val_outer)]
    val_inner_finite = val_inner[np.isfinite(val_inner)]

    if val_outer_finite.size == 0 or val_inner_finite.size == 0:
        return 0.0

    mean_outer = float(np.mean(val_outer_finite))
    mean_inner = float(np.mean(val_inner_finite))

    return mean_outer - mean_inner


def is_likely_ship_shadow(
    candidate: Dict[str, Any],
    bright_target_mask,
    adjacency_px: int = 5,
    *,
    dark_mask: Optional[np.ndarray] = None,
    min_elongation: float = 2.0,
    min_bright_target_px: int = 3,
) -> bool:
    """True when the candidate is an elongated dark region next to a bright target.

    Classic geometry: a bright high-backscatter target (ship) with a dark
    elongated radar shadow extending behind it.

    The bright-target mask MUST be supplied — it is never fabricated from the
    dark region itself. ``dark_mask`` is explicitly required to evaluate the
    candidate's own geometry.
    """
    if dark_mask is None:
        raise ValueError(
            "dark_mask is required to evaluate ship-shadow geometry; it must be "
            "supplied explicitly (never inferred from geometry)."
        )
    dark = _as_bool2d("dark_mask", dark_mask)
    bright = _as_bool2d("bright_target_mask", bright_target_mask)
    if dark.shape != bright.shape:
        raise ValueError("dark_mask and bright_target_mask must share the same shape.")

    adjacency_px = int(adjacency_px)
    if adjacency_px <= 0:
        raise ValueError("adjacency_px must be a positive integer.")

    descriptors = compute_shape_descriptors(candidate, dark)
    if descriptors["elongation"] < min_elongation:
        return False

    dilated = dilation(dark, footprint=disk(adjacency_px))
    near = dilated & bright
    if not near.any():
        return False

    # The bright feature must be a real blob, not a lone speckle pixel.
    labels = measure.label(near, connectivity=2)
    sizes = [float(p.area) for p in measure.regionprops(labels)]
    return any(size >= min_bright_target_px for size in sizes)


def is_likely_calm_water(
    candidate: Dict[str, Any],
    mask_region,
    max_edge_gradient: float = 3.0,
    *,
    dark_mask: Optional[np.ndarray] = None,
    min_area_px: Optional[int] = None,
    max_perimeter_area_ratio: float = 0.8,
) -> bool:
    """True when a large, internally uniform, diffuse dark region looks like calm water.

    ``mask_region`` must provide the SAR intensity/backscatter values needed to
    measure edge gradient (it is the intensity crop, NOT a boolean mask).
    ``dark_mask`` is explicitly required to segment the candidate's own pixels.

    Thresholds are explicit and configurable; no "oil is X m²" assumption is
    embedded.
    """
    if dark_mask is None:
        raise ValueError(
            "dark_mask is required to measure edge gradient; it must be "
            "supplied explicitly."
        )
    dark = _as_bool2d("dark_mask", dark_mask)
    intensity = np.asarray(mask_region)
    if intensity.ndim != 2:
        raise ValueError("mask_region (intensity) must be a 2-D array.")
    if intensity.shape != dark.shape:
        raise ValueError("mask_region (intensity) and dark_mask must share the same shape.")

    area_px = int(dark.sum())
    if area_px == 0:
        return False
    if min_area_px is not None and area_px < int(min_area_px):
        return False

    descriptors = compute_shape_descriptors(candidate, dark)
    if descriptors["perimeter_area_ratio"] > max_perimeter_area_ratio:
        return False

    # A smoothed boundary can be diffuse even when the region's interior is
    # substantially darker than its surroundings. Require both measurements
    # to be weak before rejecting it as calm water.
    outer = dilation(dark, footprint=disk(3)) & ~dark
    inside_values = intensity[dark & np.isfinite(intensity)]
    outside_values = intensity[outer & np.isfinite(intensity)]
    if not inside_values.size or not outside_values.size:
        return False
    regional_contrast = float(outside_values.mean() - inside_values.mean())
    return (
        _edge_gradient(intensity, dark) <= float(max_edge_gradient)
        and regional_contrast <= float(max_edge_gradient)
    )


def classify_candidate(
    candidate: Dict[str, Any],
    mask_region,
    bright_target_mask,
    *,
    adjacency_px: int = 5,
    min_elongation: float = 2.0,
    min_bright_target_px: int = 3,
    max_edge_gradient: float = 3.0,
    min_area_px: Optional[int] = None,
    max_perimeter_area_ratio: float = 0.8,
) -> str:
    """Classify a candidate into exactly one allowed label.

    ``mask_region`` is a bundle dict that provides the two required pixel
    artifacts, both aligned to ``bright_target_mask``:

        mask_region["dark_mask"]   boolean dark-mask patch for this candidate
        mask_region["intensity"]   SAR intensity/backscatter values over the patch

    Evaluation order: ship-shadow heuristic, then calm-water heuristic, else
    ``possible_slick``. ``possible_slick`` means "not rejected by these
    heuristics" — never "confirmed oil".
    """
    missing = [k for k in ("dark_mask", "intensity") if k not in mask_region]
    if missing:
        raise ValueError(
            "mask_region must be a bundle dict with 'dark_mask' and 'intensity' "
            "keys providing the candidate's pixel artifacts; missing: "
            + ", ".join(missing)
        )

    if is_likely_ship_shadow(
        candidate,
        bright_target_mask,
        adjacency_px=adjacency_px,
        dark_mask=mask_region["dark_mask"],
        min_elongation=min_elongation,
        min_bright_target_px=min_bright_target_px,
    ):
        return "likely_ship_shadow"

    if is_likely_calm_water(
        candidate,
        mask_region["intensity"],
        max_edge_gradient=max_edge_gradient,
        dark_mask=mask_region["dark_mask"],
        min_area_px=min_area_px,
        max_perimeter_area_ratio=max_perimeter_area_ratio,
    ):
        return "likely_calm_water"

    return "possible_slick"
