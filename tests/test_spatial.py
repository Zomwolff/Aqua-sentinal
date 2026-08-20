import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from shared.db.connection import close_pool, create_pool
from shared.spatial import (
    METERS_PER_KM,
    MUMBAI_AOI_BOUNDS,
    SENTINEL1_PIXEL_SIZE_M,
    area_m2_from_geometry,
    distance_km_sql,
    distance_m_sql,
    dwithin_sql,
    km_to_m,
    m_to_km,
    make_point_sql,
    mumbai_aoi_geometry,
)

LON_A, LAT_A = 73.0000, 15.0000
LON_B, LAT_B = 73.0090, 15.0000


def _run(coro):
    return asyncio.run(coro)


async def _scalar(sql, *args):
    pool = await create_pool()
    try:
        conn = await pool.acquire()
        try:
            return await conn.fetchval(sql, *args)
        finally:
            await pool.release(conn)
    finally:
        await close_pool()


def test_geometry_distance_is_meters_not_degrees():
    sql = f"""
        SELECT ST_Distance(
            {make_point_sql(LON_A, LAT_A)}::geography,
            {make_point_sql(LON_B, LAT_B)}::geography
        )
    """
    meters = _run(_scalar(sql))
    assert 900 < meters < 1100, f"expected ~1 km, got {meters}"
    raw_degrees = _run(
        _scalar(
            f"""
            SELECT ST_Distance(
                {make_point_sql(LON_A, LAT_A)},
                {make_point_sql(LON_B, LAT_B)}
            )
            """
        )
    )
    assert raw_degrees < 0.05, "raw geometry distance must be degrees, not meters"
    assert distance_km_sql("a.geom", "b.geom") == (
        f"(ST_Distance(a.geom::geography, b.geom::geography) / {METERS_PER_KM:g})"
    )


def test_dwithin_geography_radius_in_meters():
    for radius, expected in ((2000, True), (500, False)):
        inside = _run(
            _scalar(
                f"""
                SELECT ST_DWithin(
                    {make_point_sql(LON_A, LAT_A)}::geography,
                    {make_point_sql(LON_B, LAT_B)}::geography,
                    {radius}
                )
                """
            )
        )
        assert inside is expected, f"radius {radius} m expected {expected}"


def test_dwithin_geometry_without_cast_is_degree_bug():
    inside = _run(
        _scalar(
            f"""
            SELECT ST_DWithin(
                {make_point_sql(LON_A, LAT_A)},
                {make_point_sql(LON_B, LAT_B)},
                500
            )
            """
        )
    )
    assert inside is True, "raw geometry ST_DWithin treats 500 as 500 degrees"


def test_intersection_stays_geometry_based():
    overlap = _run(
        _scalar(
            """
            SELECT ST_Intersects(
                ST_GeomFromText('POLYGON((73.8 15.1, 74.0 15.1, 74.0 15.3, 73.8 15.3, 73.8 15.1))', 4326),
                ST_GeomFromText('POLYGON((73.9 15.2, 74.1 15.2, 74.1 15.4, 73.9 15.4, 73.9 15.2))', 4326)
            )
            """
        )
    )
    disjoint = _run(
        _scalar(
            """
            SELECT ST_Intersects(
                ST_GeomFromText('POLYGON((72.0 14.0, 72.2 14.0, 72.2 14.2, 72.0 14.2, 72.0 14.0))', 4326),
                ST_GeomFromText('POLYGON((73.9 15.2, 74.1 15.2, 74.1 15.4, 73.9 15.4, 73.9 15.2))', 4326)
            )
            """
        )
    )
    assert overlap is True
    assert disjoint is False


def test_longitude_latitude_order():
    wkt = _run(_scalar(f"SELECT ST_AsText({make_point_sql(73.92, 15.20)})"))
    assert wkt == "POINT(73.92 15.2)", f"got {wkt}"
    x = _run(_scalar(f"SELECT ST_X({make_point_sql(73.92, 15.20)})"))
    y = _run(_scalar(f"SELECT ST_Y({make_point_sql(73.92, 15.20)})"))
    assert x == 73.92
    assert y == 15.20


def test_unit_conversion_helpers():
    assert km_to_m(10) == 10000.0
    assert m_to_km(1500) == 1.5
    assert METERS_PER_KM == 1000.0


def test_sql_helpers_use_geography_cast():
    assert distance_m_sql("v.geom", "s.centroid") == (
        "ST_Distance(v.geom::geography, s.centroid::geography)"
    )
    assert dwithin_sql("v.geom", "s.centroid", 10000) == (
        "ST_DWithin(v.geom::geography, s.centroid::geography, 10000)"
    )
    assert make_point_sql(73.92, 15.20) == "ST_SetSRID(ST_MakePoint(73.92, 15.2), 4326)"


def test_mumbai_aoi_geometry_returns_correct_bounds():
    min_lon, min_lat, max_lon, max_lat = MUMBAI_AOI_BOUNDS
    assert MUMBAI_AOI_BOUNDS == (72.75, 18.85, 73.05, 19.15)
    assert mumbai_aoi_geometry() == (
        f"POLYGON(({min_lon:g} {min_lat:g}, {max_lon:g} {min_lat:g}, "
        f"{max_lon:g} {max_lat:g}, {min_lon:g} {max_lat:g}, "
        f"{min_lon:g} {min_lat:g}))"
    )
    assert mumbai_aoi_geometry().startswith("POLYGON((")


def test_sentinel1_pixel_size_constant():
    assert SENTINEL1_PIXEL_SIZE_M == 10.0


def test_area_sql_uses_geography_cast_and_preserves_srid():
    assert area_m2_from_geometry("x.geom") == (
        "ST_Area(ST_SetSRID(x.geom, 4326)::geography)"
    )
    assert area_m2_from_geometry("x.geom", srid=4326) == (
        "ST_Area(ST_SetSRID(x.geom, 4326)::geography)"
    )
    assert "::geography" in area_m2_from_geometry("x.geom")


def test_area_m2_from_geometry_is_square_meters_not_degrees_sq():
    box = "POLYGON((72.80 18.90, 72.81 18.90, 72.81 18.91, 72.80 18.91, 72.80 18.90))"
    meters = _run(_scalar(f"SELECT {area_m2_from_geometry(box)}"))
    # Hand-computed geodesic approx: 0.01deg lat (~1113.2 m) x 0.01deg lon
    # (~1053.2 m at cos(18.9deg)) ~= 1.172M m^2.
    assert 1.05e6 < meters < 1.30e6, f"expected ~1.17M m^2, got {meters}"
    raw = _run(
        _scalar(
            f"SELECT ST_Area(ST_GeomFromText('{box}', 4326))"
        )
    )
    assert raw < 0.001, "raw geometry area must be degrees^2, not m^2"


def test_mumbai_aoi_area_is_sane():
    sql = area_m2_from_geometry(mumbai_aoi_geometry())
    meters = _run(_scalar(f"SELECT {sql}"))
    # 0.3deg x 0.3deg near lat 19: approx 1.05e9 m^2.
    assert 0.9e9 < meters < 1.2e9, f"unexpected AOI area {meters}"