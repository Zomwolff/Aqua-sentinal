import sys
from pathlib import Path

import numpy as np
import pytest

SERVICE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_ROOT))

from app.shape_filters import (
    ALLOWED_LABELS,
    classify_candidate,
    compute_shape_descriptors,
    is_likely_calm_water,
    is_likely_ship_shadow,
)


def _candidate(geometry_lon_lat=True):
    # GeoJSON Polygon with (longitude, latitude) ordering (EPSG:4326).
    ring = [
        [72.75145, 19.14855],
        [72.75155, 19.14855],
        [72.75155, 19.14865],
        [72.75145, 19.14865],
        [72.75145, 19.14855],
    ]
    return {
        "candidate_id": "00000000-0000-0000-0000-000000000001",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "area_m2": 10000.0,
        "pixel_count": 100,
        "centroid": {"type": "Point", "coordinates": [72.7515, 19.1486]},
    }


def _dark_mask_for(intensity, threshold=0.0):
    return np.asarray(intensity) < threshold


# ──────────────────────────────────────────────────────────────────────────────
# Ship shadow
# ──────────────────────────────────────────────────────────────────────────────

def _ship_shadow_scene(bright_distance=0):
    """Intensity scene with a bright vessel and an elongated dark shadow.

    ``bright_distance`` separates the bright target from the shadow (rows).
    """
    scene = np.zeros((60, 60), dtype=np.float64)
    top = 8 + bright_distance
    scene[top : top + 10, 8:21] = 2.0  # bright target (ship)
    scene[20 : 20 + 17, 10:16] = -3.0  # dark elongated shadow
    shadow = np.zeros((60, 60), dtype=bool)
    shadow[20 : 20 + 17, 10:16] = True
    bright = scene > 0.5
    return scene, shadow, bright


def test_ship_shadow_classification_with_nearby_bright_target():
    scene, shadow, bright = _ship_shadow_scene(bright_distance=0)
    candidate = _candidate()

    assert is_likely_ship_shadow(
        candidate, bright, adjacency_px=5, dark_mask=shadow
    )
    label = classify_candidate(
        candidate,
        {"dark_mask": shadow, "intensity": scene},
        bright,
    )
    assert label == "likely_ship_shadow"


def test_ship_shadow_requires_actual_nearby_bright_target():
    candidate = _candidate()

    # No bright pixels at all -> not a ship shadow.
    scene, shadow, _ = _ship_shadow_scene()
    empty_bright = np.zeros_like(shadow)
    assert not is_likely_ship_shadow(
        candidate, empty_bright, adjacency_px=5, dark_mask=shadow
    )
    assert (
        classify_candidate(candidate, {"dark_mask": shadow, "intensity": scene}, empty_bright)
        != "likely_ship_shadow"
    )

    # Bright target starts 40 rows away (well beyond adjacency_px) -> not adjacent.
    scene, shadow, bright = _ship_shadow_scene(bright_distance=40)
    assert not is_likely_ship_shadow(
        candidate, bright, adjacency_px=5, dark_mask=shadow
    )


def test_ship_shadow_requires_elongated_dark_region():
    candidate = _candidate()
    scene = np.zeros((60, 60), dtype=np.float64)
    scene[8:18, 8:21] = 2.0  # bright target
    # compact (non-elongated) dark blob, adjacent
    scene[20:30, 10:20] = -3.0
    shadow = scene < 0.0
    bright = scene > 0.5

    assert compute_shape_descriptors(candidate, shadow)["elongation"] < 2.0
    assert not is_likely_ship_shadow(
        candidate, bright, adjacency_px=5, dark_mask=shadow, min_elongation=2.0
    )


# ──────────────────────────────────────────────────────────────────────────────
# Calm water
# ──────────────────────────────────────────────────────────────────────────────

def _calm_water_scene(contrast=0.02):
    rng = np.random.default_rng(2026)
    intensity = np.full((60, 60), 0.0, dtype=np.float64)
    patch = np.full((35, 55), -1.3, dtype=np.float64)
    patch += rng.normal(0.0, 0.02, patch.shape) if contrast < 0.1 else rng.normal(0.0, 0.6, patch.shape)
    intensity[3:38, 3:58] = patch
    mask = np.zeros((60, 60), dtype=bool)
    mask[3:38, 3:58] = True
    return intensity, mask


def test_calm_water_classification_uses_low_contrast_intensity():
    candidate = _candidate()
    intensity, mask = _calm_water_scene(contrast=0.02)
    empty = np.zeros_like(mask)

    assert is_likely_calm_water(
        candidate, intensity, contrast_threshold=0.15, dark_mask=mask, min_area_px=200
    )
    assert (
        classify_candidate(
            candidate,
            {"dark_mask": mask, "intensity": intensity},
            empty,
            min_area_px=200,
        )
        == "likely_calm_water"
    )


def test_calm_water_uses_intensity_not_polygon_area_alone():
    candidate = _candidate()

    # Identical candidate geometry / mask area; only the intensity differs.
    flat_intensity, mask = _calm_water_scene(contrast=0.02)
    noisy_intensity, _ = _calm_water_scene(contrast=0.6)
    empty = np.zeros_like(mask)

    assert compute_shape_descriptors(candidate, mask)["area_px"] == compute_shape_descriptors(
        candidate, mask
    )["area_px"]
    assert is_likely_calm_water(candidate, flat_intensity, dark_mask=mask)
    assert not is_likely_calm_water(candidate, noisy_intensity, dark_mask=mask)

    assert classify_candidate(candidate, {"dark_mask": mask, "intensity": flat_intensity}, empty) == "likely_calm_water"
    assert classify_candidate(candidate, {"dark_mask": mask, "intensity": noisy_intensity}, empty) != "likely_calm_water"


# ──────────────────────────────────────────────────────────────────────────────
# Possible slick
# ──────────────────────────────────────────────────────────────────────────────

def test_possible_slick_when_no_lookalike_heuristic_applies():
    candidate = _candidate()
    scene = np.zeros((60, 60), dtype=np.float64)
    scene[20:30, 20:30] = -2.0  # compact dark blob, moderate contrast
    scene[20:30, 20:30] += np.random.default_rng(1).normal(0.0, 0.5, (10, 10))
    blob = np.zeros((60, 60), dtype=bool)
    blob[20:30, 20:30] = True
    empty = np.zeros_like(blob)

    label = classify_candidate(candidate, {"dark_mask": blob, "intensity": scene}, empty)
    assert label == "possible_slick"


# ──────────────────────────────────────────────────────────────────────────────
# Degenerate / safety
# ──────────────────────────────────────────────────────────────────────────────

def test_zero_area_region_does_not_divide_by_zero():
    candidate = _candidate()
    empty = np.zeros((20, 20), dtype=bool)
    intensity = np.zeros((20, 20), dtype=np.float64)

    descriptors = compute_shape_descriptors(candidate, empty)
    assert descriptors["elongation"] == 1.0
    assert descriptors["perimeter_area_ratio"] == 0.0
    assert descriptors["solidity"] == 0.0

    # No crash, no ZeroDivisionError; falls through to possible_slick.
    label = classify_candidate(
        candidate, {"dark_mask": empty, "intensity": intensity}, empty
    )
    assert label == "possible_slick"


def test_single_pixel_region_is_safe():
    candidate = _candidate()
    px = np.zeros((10, 10), dtype=bool)
    px[3, 4] = True
    intensity = np.zeros((10, 10), dtype=np.float64)
    intensity[3, 4] = -2.0

    descriptors = compute_shape_descriptors(candidate, px)
    assert descriptors["area_px"] == 1
    assert np.isfinite(descriptors["elongation"])


def test_classify_never_returns_unsupported_label():
    scenarios = [
        _ship_shadow_scene(),
    ]
    for scene, mask, bright in scenarios:
        label = classify_candidate(
            _candidate(),
            {"dark_mask": mask, "intensity": scene},
            bright,
            min_area_px=200,
        )
        assert label in ALLOWED_LABELS

    for intensity, mask in (_calm_water_scene(0.02), _calm_water_scene(0.6)):
        label = classify_candidate(
            _candidate(),
            {"dark_mask": mask, "intensity": intensity},
            np.zeros_like(mask),
            min_area_px=200,
        )
        assert label in ALLOWED_LABELS


def test_missing_dark_mask_is_rejected_not_invented():
    candidate = _candidate()
    bright = np.zeros((10, 10), dtype=bool)
    with pytest.raises(ValueError):
        is_likely_ship_shadow(candidate, bright, dark_mask=None)
    with pytest.raises(ValueError):
        is_likely_calm_water(candidate, np.zeros((10, 10)), dark_mask=None)


# ──────────────────────────────────────────────────────────────────────────────
# Coordinate handling
# ──────────────────────────────────────────────────────────────────────────────

def test_classification_preserves_longitude_latitude_geometry():
    candidate = _candidate()
    scene, shadow, bright = _ship_shadow_scene()
    geometry_before = candidate["geometry"]["coordinates"][0][:]

    label = classify_candidate(candidate, {"dark_mask": shadow, "intensity": scene}, bright)

    ring = candidate["geometry"]["coordinates"][0]
    assert ring == geometry_before  # unchanged: already (lon, lat)
    for lon, lat in ring:
        assert 72.0 <= lon <= 74.0, "first coordinate must be longitude"
        assert 18.0 <= lat <= 20.0, "second coordinate must be latitude"
    assert label in ALLOWED_LABELS