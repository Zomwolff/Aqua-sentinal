import sys
from pathlib import Path

import numpy as np
import pytest

SERVICE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SERVICE_ROOT.resolve().parent.parent

import sys

sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(REPO_ROOT))
# In containers, shared/ is the merged top-level + services/shared package;
# on the host, expose the real services/ tree so shared.artifacts resolves.
sys.path.insert(0, str(REPO_ROOT / "services"))

from shared.artifacts import (
    crop_region,
    geometry_to_pixel_bbox,
    load_scene_artifact,
    save_scene_artifact,
    scene_artifact_path,
)

# Mumbai-style affine: xres 0.0001 deg/px east, yres -0.0001 deg/px (rows south).
AFFINE = [0.0001, 0.0, 72.75, 0.0, -0.0001, 19.15]
SHAPE = (30, 60)


def _pixel_square_polygon():
    # A 10x10 pixel square, rows 10..20, cols 40..50, as a lon/lat polygon.
    lon40, lon50 = 72.754, 72.755
    lat10, lat20 = 19.149, 19.148
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon40, lat10],
                [lon50, lat10],
                [lon50, lat20],
                [lon40, lat20],
                [lon40, lat10],
            ]
        ],
    }


def test_scene_artifact_path_sanitizes_scene_id():
    root = "/tmp/artifacts"
    p = scene_artifact_path(root, "COPERNICUS/S1_GRD/S1A_ABC")
    assert p.startswith("/tmp/artifacts/")
    assert "/" not in p[len(root) + 1 :]
    assert "COPERNICUS_S1_GRD_S1A_ABC" in p


def test_save_load_roundtrip(tmp_path):
    filtered = np.arange(12, dtype=np.float64).reshape(3, 4)
    cleaned = np.array([[0, 0, 1, 1], [0, 1, 1, 0], [0, 0, 0, 0]], dtype=bool)
    bright = np.array([[1, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 1]], dtype=bool)

    root = str(tmp_path)
    scene_id = "COPERNICUS/S1_GRD/SCENE_4711"
    save_scene_artifact(
        root,
        scene_id,
        filtered_image=filtered,
        cleaned_mask=cleaned,
        bright_target_mask=bright,
        affine=AFFINE,
        shape=SHAPE,
        crs="EPSG:4326",
    )

    artifact = load_scene_artifact(root, scene_id)
    assert artifact is not None
    np.testing.assert_array_equal(artifact["filtered_image"], filtered)
    np.testing.assert_array_equal(artifact["cleaned_mask"], cleaned)
    np.testing.assert_array_equal(artifact["bright_target_mask"], bright)
    assert artifact["metadata"]["scene_id"] == scene_id
    assert artifact["metadata"]["affine"] == AFFINE
    assert artifact["metadata"]["shape"] == [30, 60]
    assert artifact["metadata"]["crs"] == "EPSG:4326"


def test_load_missing_artifact_returns_none(tmp_path):
    assert load_scene_artifact(str(tmp_path), "NOPE/SCENE") is None


def test_geometry_to_pixel_bbox_is_correct_and_not_reversed():
    poly = _pixel_square_polygon()
    bbox = geometry_to_pixel_bbox(poly, AFFINE, padding=0, shape=SHAPE)
    # rows 10..20, cols 40..50 exactly.
    assert bbox == (10, 20, 40, 50)


def test_geometry_to_pixel_bbox_reversal_would_be_detected():
    poly = _pixel_square_polygon()
    bbox = geometry_to_pixel_bbox(poly, AFFINE, padding=0, shape=SHAPE)
    r0, r1, c0, c1 = bbox

    # Longitude range 72.754..72.755 MUST map to columns ~40..50, and latitude
    # range 19.149..19.148 MUST map to rows ~10..20. A lon/lat reversal would
    # produce wildly different bounds.
    assert c0 >= 40 and c1 <= 50, "longitude must drive the column axis"
    assert r0 >= 10 and r1 <= 20, "latitude must drive the row axis"


def test_padding_clamps_to_shape():
    poly = _pixel_square_polygon()
    padded = geometry_to_pixel_bbox(poly, AFFINE, padding=5, shape=SHAPE)
    assert padded == (5, 25, 35, 55)

    # A polygon straddling the top scene edge (rows -2..2) is clamped to row 0.
    half_out = {
        "type": "Polygon",
        "coordinates": [
            [
                [72.7520, 19.1502],
                [72.7525, 19.1502],
                [72.7525, 19.1498],
                [72.7520, 19.1498],
                [72.7520, 19.1502],
            ]
        ],
    }
    clamped = geometry_to_pixel_bbox(half_out, AFFINE, padding=3, shape=SHAPE)
    assert clamped[0] == 0
    assert clamped[2] == 17

    # Entirely outside the raster -> explicit error, not silent out-of-range.
    outside = {
        "type": "Polygon",
        "coordinates": [
            [
                [72.0, 18.0],
                [72.01, 18.0],
                [72.01, 18.01],
                [72.0, 18.01],
                [72.0, 18.0],
            ]
        ],
    }
    with pytest.raises(ValueError):
        geometry_to_pixel_bbox(outside, AFFINE, padding=0, shape=SHAPE)


def test_crop_region():
    array = np.arange(9, dtype=np.float64).reshape(3, 3)
    cropped = crop_region(array, (1, 3, 0, 2))
    np.testing.assert_array_equal(cropped, array[1:3, 0:2])