import asyncio
import sys
from pathlib import Path

import numpy as np
import pytest
from affine import Affine

SERVICE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SERVICE_ROOT.resolve().parent.parent
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from app.morphology import clean_mask
from app.polygonize import extract_candidates
from app.segmentation import dark_region_mask
from shared.spatial.geo import area_m2_from_geometry


def _run(coro):
    return asyncio.run(coro)


async def _postgis_available() -> bool:
    import asyncpg

    from shared.db.connection import get_dsn

    try:
        conn = await asyncpg.connect(get_dsn(), timeout=2)
        try:
            await conn.fetchval("SELECT postgis_version()")
            return True
        except Exception:
            return False
        finally:
            await conn.close()
    except Exception:
        return False


POSTGIS_AVAILABLE = _run(_postgis_available())


def _mumbai_transform():
    # xres=0.0001 deg/px east, yres=-0.0001 deg/px (rows grow south).
    return Affine(0.0001, 0.0, 72.75, 0.0, -0.0001, 19.15)


def test_dark_region_mask_detects_large_low_backscatter_region():
    rng = np.random.default_rng(42)
    image = rng.normal(0.0, 0.01, size=(128, 128))
    image[30:100, 30:100] = -20.0  # large dark (low-dB) region

    mask = dark_region_mask(image)

    inside = mask[30:100, 30:100].copy()
    outside = mask.copy()
    outside[30:100, 30:100] = False
    assert inside.mean() > 0.95, "dark region must be detected as dark"
    assert outside.mean() < 0.05, "background must not be dark"


def test_dark_region_mask_excludes_bright_region():
    rng = np.random.default_rng(43)
    image = rng.normal(0.0, 0.01, size=(128, 128))
    image[40:80, 40:80] = 20.0  # bright high-backscatter region

    mask = dark_region_mask(image)

    assert not mask[40:80, 40:80].any(), "bright region must NOT be dark"


def test_dark_region_polarity_lower_backscatter_is_dark():
    # dB convention: lower value = darker backscatter.
    image = np.zeros((64, 128))
    image[:, :64] = -10.0  # dark half
    image[:, 64:] = +10.0  # bright half

    mask = dark_region_mask(image)

    assert mask[:, :64].all(), "low-dB pixels must be dark"
    assert not mask[:, 64:].any(), "high-dB pixels must not be dark"


def test_mixed_scene_masks_are_distinct():
    rng = np.random.default_rng(44)
    image = rng.normal(0.0, 0.01, size=(128, 128))
    image[20:60, 20:60] = -12.0  # dark region
    image[80:100, 80:100] = 15.0  # bright target
    bright_threshold = 1.0

    dark = dark_region_mask(image)
    bright = image > bright_threshold

    assert dark[20:60, 20:60].mean() > 0.9
    assert bright[80:100, 80:100].all()
    # distinct masks: a pixel cannot be both dark and bright
    assert not (dark & bright).any()


def test_dark_region_mask_rejects_unsupported_method():
    with pytest.raises(ValueError, match="method"):
        dark_region_mask(np.zeros((8, 8)), method="kmeans")


def test_dark_region_mask_requires_numeric_2d():
    with pytest.raises(ValueError):
        dark_region_mask(np.zeros(4))
    with pytest.raises(ValueError):
        dark_region_mask(np.empty((0, 0)))


def test_dark_region_candidate_generation_survives_pipeline():
    # Deterministic large dark region -> segmentation -> morphology -> polygon
    rng = np.random.default_rng(45)
    image = rng.normal(0.0, 0.01, size=(128, 128))
    image[30:90, 30:90] = -20.0

    dark = dark_region_mask(image)
    cleaned = clean_mask(dark, open_size=3, close_size=5)
    candidates = extract_candidates(
        cleaned,
        _mumbai_transform(),
        min_area_m2=40000.0,
        pixel_size_m=10.0,
    )

    assert len(candidates) >= 1
    assert all(c["pixel_count"] > 0 for c in candidates)
    assert all(c["geometry"]["type"] == "Polygon" for c in candidates)

    if POSTGIS_AVAILABLE:
        import json

        import asyncpg

        from shared.db.connection import get_dsn

        async def _geography_area(geojson_json):
            conn = await asyncpg.connect(get_dsn(), timeout=2)
            try:
                sql = f"SELECT {area_m2_from_geometry('ST_GeomFromGeoJSON($1)')}"
                return float(await conn.fetchval(sql, geojson_json))
            finally:
                await conn.close()

        candidate = candidates[0]
        area_m2 = _run(_geography_area(json.dumps(candidate["geometry"])))
        # 60x60 pixels of ~10 m ground range -> tens of thousands of m².
        assert 20000.0 < area_m2 < 900000.0