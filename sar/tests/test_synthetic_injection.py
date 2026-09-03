import sys
from pathlib import Path

import numpy as np
import pytest

# SAR refactor: this test moved from services/data-ingestion/ to sar/tests/.
# Acquisition code now lives in sar/acquisition/.
SAR_ACQ_ROOT = Path(__file__).resolve().parent.parent / "acquisition"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SAR_ACQ_ROOT))
sys.path.insert(0, str(REPO_ROOT))

try:
    from app.synthetic_injection import (
        inject_geotiff_if_enabled,
        inject_synthetic_slick,
        resolve_synthetic_enabled,
    )
except ImportError:  # repo-root layout: sar.acquisition package
    from sar.acquisition.synthetic_injection import (
        inject_geotiff_if_enabled,
        inject_synthetic_slick,
        resolve_synthetic_enabled,
    )


def test_injection_never_mutates_input():
    rng = np.random.default_rng(7)
    image = rng.normal(-1.0, 0.3, size=(64, 64))
    original = image.copy()
    result, _ = inject_synthetic_slick(image, (32, 32))
    assert np.array_equal(original, image), "input must be unchanged"
    assert not np.array_equal(original, result), "target region must change"


def test_injection_returns_a_copy():
    rng = np.random.default_rng(8)
    image = rng.normal(-1.0, 0.3, size=(32, 32))
    result, _ = inject_synthetic_slick(image, (16, 16))
    assert result is not image
    assert np.shares_memory(result, image) is False


def test_outside_region_is_preserved_within_blur_tolerance():
    rng = np.random.default_rng(9)
    image = rng.normal(-1.0, 0.3, size=(64, 64))
    result, _ = inject_synthetic_slick(image, (32, 32), length_px=20, width_px=6)
    # Far-corner pixels are far outside the blurred ellipse.
    far = (slice(0, 8), slice(0, 8))
    assert np.allclose(image[far], result[far], atol=1e-6)


def test_injection_metadata_records_parameters():
    image = np.zeros((48, 48))
    result, metadata = inject_synthetic_slick(
        image, (24, 24), length_px=30, width_px=6, angle_deg=45.0, darkness_db=-5.0
    )
    assert metadata["synthetic"] is True
    inj = metadata["injection"]
    assert inj["length_px"] == 30.0
    assert inj["width_px"] == 6.0
    assert inj["angle_deg"] == 45.0
    assert inj["darkness_db"] == -5.0
    assert inj["center"] == [24, 24]
    # Injected region is darker than the (zero) background.
    assert result[24, 24] < 0.0
    assert result.min() < 0.0


def test_angle_changes_orientation():
    image = np.zeros((64, 64))
    result0, _ = inject_synthetic_slick(image, (32, 32), length_px=40, width_px=8, angle_deg=0.0)
    result90, _ = inject_synthetic_slick(image, (32, 32), length_px=40, width_px=8, angle_deg=90.0)
    # Horizontal vs vertical elongation: different darkness distribution.
    assert not np.allclose(result0, result90)


def test_dtype_preserved():
    image = np.zeros((32, 32), dtype=np.float32)
    result, _ = inject_synthetic_slick(image, (16, 16))
    assert result.dtype == np.float32


def test_resolve_synthetic_enabled_precedence():
    # Default: disabled unless something says otherwise.
    assert resolve_synthetic_enabled(None, None) is False
    assert resolve_synthetic_enabled(None, "false") is False
    # Environment enables.
    assert resolve_synthetic_enabled(None, "true") is True
    assert resolve_synthetic_enabled(None, "1") is True
    assert resolve_synthetic_enabled(None, "on") is True
    # Explicit field overrides environment in both directions.
    assert resolve_synthetic_enabled(True, "false") is True
    assert resolve_synthetic_enabled(False, "true") is False


def test_inject_geotiff_disabled_returns_original(tmp_path):
    import rasterio

    path = str(tmp_path / "scene.tif")
    with rasterio.open(
        path, "w", driver="GTiff", height=16, width=16, count=1, dtype="float32",
        crs="EPSG:4326",
    ) as ds:
        ds.write(np.zeros((16, 16), dtype=np.float32), 1)

    returned, meta = inject_geotiff_if_enabled(path, "scene_id", explicit=False)
    assert returned == path
    assert meta["is_synthetic"] is False


def test_inject_geotiff_enabled_writes_new_file(tmp_path):
    import rasterio

    path = str(tmp_path / "scene.tif")
    with rasterio.open(
        path, "w", driver="GTiff", height=32, width=32, count=1, dtype="float32",
        crs="EPSG:4326",
    ) as ds:
        ds.write(np.zeros((32, 32), dtype=np.float32), 1)

    returned, meta = inject_geotiff_if_enabled(path, "scene_id", explicit=True)
    assert returned != path
    assert returned.endswith("_synthetic.tif")
    assert meta["is_synthetic"] is True
    assert meta["synthetic"] is True
    with rasterio.open(path) as ds:
        original = ds.read(1)
    with rasterio.open(returned) as ds:
        modified = ds.read(1)
    assert not np.array_equal(original, modified)
    assert modified.min() < original.min()


def test_sar_clean_message_metadata_carries_synthetic_flag(tmp_path):
    import json

    import rasterio

    path = str(tmp_path / "scene.tif")
    with rasterio.open(
        path, "w", driver="GTiff", height=32, width=32, count=1, dtype="float32",
        crs="EPSG:4326",
    ) as ds:
        ds.write(np.zeros((32, 32), dtype=np.float32), 1)

    synthetic_path, meta = inject_geotiff_if_enabled(path, "scene_id", explicit=True)
    payload = json.dumps(meta)
    decoded = json.loads(payload)
    assert decoded["is_synthetic"] is True
    assert synthetic_path.endswith("_synthetic.tif")