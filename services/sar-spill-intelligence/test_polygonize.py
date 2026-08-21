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

from app.morphology import estimate_min_area_px
from app.polygonize import extract_candidates
from app.worker import _reproject_candidates_to_wgs84
from shared.spatial.geo import area_m2_from_geometry


def _run(coro):
    return asyncio.run(coro)


def _rect_mask(rows=20, cols=30):
    mask = np.zeros((rows, cols), dtype=bool)
    mask[5:15, 10:20] = True
    return mask


def _mumbai_transform():
    # xres=0.0001 deg/px east, yres=-0.0001 deg/px (rows grow south).
    # origin (col=0, row=0) at lon 72.75, lat 19.15.
    return Affine(0.0001, 0.0, 72.75, 0.0, -0.0001, 19.15)


async def _postgis_available() -> bool:
    """Probe PostGIS connectivity without creating a shared pool."""
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


def _rect_polygon_coords(candidate, lon_range=(72.7, 73.1), lat_range=(18.8, 19.2)):
    geom = candidate["geometry"]
    assert geom["type"] == "Polygon"
    assert len(geom["coordinates"]) >= 1
    ring = geom["coordinates"][0]
    assert len(ring) >= 4
    for lon, lat in ring:
        assert lon_range[0] <= lon <= lon_range[1], "first coord must be longitude"
        assert lat_range[0] <= lat <= lat_range[1], "second coord must be latitude"
    return ring


def test_rectangular_blob_structure_and_centroid():
    mask = _rect_mask()
    transform = _mumbai_transform()

    candidates = extract_candidates(
        mask,
        transform,
        min_area_m2=5000.0,
        pixel_size_m=10.0,
    )

    assert len(candidates) == 1
    candidate = candidates[0]

    assert candidate["pixel_count"] == 100
    assert candidate["area_m2"] is None  # pure geometry step; area resolved by worker

    geom = candidate["geometry"]
    assert geom["type"] == "Polygon"
    assert len(geom["coordinates"]) >= 1
    ring = geom["coordinates"][0]
    assert ring[0] == ring[-1], "polygon ring must be closed"
    assert len(ring) >= 4

    for lon, lat in ring:
        assert 72.7 <= lon <= 73.1, f"expected longitude, got {lon}"
        assert 18.8 <= lat <= 19.2, f"expected latitude, got {lat}"

    lon_cent, lat_cent = candidate["centroid"]["coordinates"]
    # rectangle occupies mask rows 5..14 and cols 10..19, centroid (9.5, 14.5)
    assert lon_cent == pytest.approx(72.75145, abs=1e-9)
    assert lat_cent == pytest.approx(19.14905, abs=1e-9)


@pytest.mark.skipif(
    not POSTGIS_AVAILABLE,
    reason="PostGIS unreachable; geography-cast area assertion requires it",
)
def test_rectangular_blob_area_uses_geography_cast():
    """area_m2 must come from area_m2_from_geometry, never degree**2 math."""
    import json

    import asyncpg

    from shared.db.connection import get_dsn

    candidates = extract_candidates(
        _rect_mask(),
        _mumbai_transform(),
        min_area_m2=5000.0,
        pixel_size_m=10.0,
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["area_m2"] is None

    async def _geography_area(geojson_json):
        conn = await asyncpg.connect(get_dsn(), timeout=2)
        try:
            sql = f"SELECT {area_m2_from_geometry('ST_GeomFromGeoJSON($1)')}"
            return float(await conn.fetchval(sql, geojson_json))
        finally:
            await conn.close()

    # Worker resolution step (AREA_M2_SQL) is built from the same helper.
    area_m2 = _run(_geography_area(json.dumps(candidate["geometry"])))

    # 100 ground-range pixels of 10 m -> ~10 000 m²; small geodesic deviation
    # from the lon/col scaling near lat 19 is expected.
    assert 9000.0 < area_m2 < 13000.0

    # Guard against a regression to degree**2: the raw degree**2 of this ring is
    # ~1e-6, several orders of magnitude below the geography result.
    ring = candidate["geometry"]["coordinates"][0]
    lon_span = max(p[0] for p in ring) - min(p[0] for p in ring)
    lat_span = max(p[1] for p in ring) - min(p[1] for p in ring)
    degree2_area = lon_span * lat_span
    assert area_m2 / degree2_area > 1e6


def test_sub_threshold_component_is_excluded():
    mask = np.zeros((40, 40), dtype=bool)
    mask[5:25, 5:25] = True  # 400 px -> ~40 000 m², above threshold
    mask[30:33, 30:33] = True  # 9 px -> ~900 m², below threshold

    candidates = extract_candidates(
        mask,
        _mumbai_transform(),
        min_area_m2=20000.0,
        pixel_size_m=10.0,
    )

    assert len(candidates) == 1
    assert candidates[0]["pixel_count"] == 400

    ring = _rect_polygon_coords(candidates[0])
    lon_cent, lat_cent = candidates[0]["centroid"]["coordinates"]
    # 400-px rectangle occupies rows 5..24, cols 5..24 -> centroid (14.5, 14.5)
    assert lon_cent == pytest.approx(72.75145, abs=1e-9)
    assert lat_cent == pytest.approx(19.14855, abs=1e-9)
    assert ring


def test_polygon_coordinates_are_longitude_latitude_not_reversed():
    candidates = extract_candidates(
        _rect_mask(),
        _mumbai_transform(),
        min_area_m2=5000.0,
        pixel_size_m=10.0,
    )
    assert len(candidates) == 1
    ring = candidates[0]["geometry"]["coordinates"][0]

    # Longitudes live around 72.75..72.76; latitudes around 19.14..19.15.
    # If a (latitude, longitude) bug crept in these would land ~72 reversed.
    lons = [pt[0] for pt in ring]
    lats = [pt[1] for pt in ring]
    assert any(72.70 <= lon <= 72.80 for lon in lons)
    assert any(19.10 <= lat <= 19.20 for lat in lats)
    assert not any(18.0 <= lat <= 20.0 for lat in lons), "first coord must be lon"
    assert not any(72.0 <= lon <= 74.0 for lon in lats), "second coord must be lat"


def test_utm_candidates_are_reprojected_to_wgs84_and_oriented():
    candidates = extract_candidates(
        _rect_mask(),
        Affine(10.0, 0.0, 263000.0, 0.0, -10.0, 2095000.0),
        min_area_m2=5000.0,
        pixel_size_m=10.0,
    )

    _reproject_candidates_to_wgs84(candidates, "EPSG:32643")

    assert len(candidates) == 1
    ring = candidates[0]["geometry"]["coordinates"][0]
    assert all(72.0 <= lon <= 74.0 for lon, _ in ring)
    assert all(18.0 <= lat <= 20.0 for _, lat in ring)
    lon, lat = candidates[0]["centroid"]["coordinates"]
    assert 72.0 <= lon <= 74.0
    assert 18.0 <= lat <= 20.0


def test_estimate_min_area_px_requires_explicit_min_area_m2():
    with pytest.raises(ValueError, match="min_area_m2 is required"):
        estimate_min_area_px(pixel_size_m=10.0, min_area_m2=None)

    with pytest.raises(ValueError):
        estimate_min_area_px(pixel_size_m=10.0, min_area_m2=0)

    assert estimate_min_area_px(pixel_size_m=10.0, min_area_m2=10_000.0) == 100
    assert estimate_min_area_px(pixel_size_m=10.0, min_area_m2=5_000.0) == 50
