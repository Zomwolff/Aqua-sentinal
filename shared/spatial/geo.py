from shared.spatial.constants import MUMBAI_AOI_BOUNDS, METERS_PER_KM, SRID


def mumbai_aoi_geometry() -> str:
    """WKT POLYGON (EPSG:4326) for the SIH260361 Option 1 study area.

    Built from MUMBAI_AOI_BOUNDS in lon/lat order (never reversed, per
    docs/spatial.md). Returns WKT so it drops straight into PostGIS.
    """
    min_lon, min_lat, max_lon, max_lat = MUMBAI_AOI_BOUNDS
    return (
        f"POLYGON(({_fmt(min_lon)} {_fmt(min_lat)}, "
        f"{_fmt(max_lon)} {_fmt(min_lat)}, "
        f"{_fmt(max_lon)} {_fmt(max_lat)}, "
        f"{_fmt(min_lon)} {_fmt(max_lat)}, "
        f"{_fmt(min_lon)} {_fmt(min_lat)}))"
    )


def area_m2_from_geometry(geom, srid=SRID) -> str:
    """SQL for ST_Area via PostGIS geography semantics, returning square metres.

    Per docs/spatial.md, metric math MUST go through a geography cast —
    EPSG:4326 geometry stores degrees, not meters. This emits
    ST_Area(ST_SetSRID(<geom>, <srid>)::geography), preserving the geometry's
    SRID and using PostGIS geodesic area. Do NOT "simplify" this to a Shapely
    or projected-CRS calculation; a ~0.01deg box is ~0.0001 degree^2 raw but
    ~1.17M m^2 via geography.
    """
    return f"ST_Area(ST_SetSRID({geom}, {srid})::geography)"


def _fmt(value) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):g}"
    return str(value)


def make_point_sql(longitude, latitude) -> str:
    """Build POINT(longitude latitude) with SRID 4326. Longitude comes first."""
    return f"ST_SetSRID(ST_MakePoint({_fmt(longitude)}, {_fmt(latitude)}), {SRID})"


def geography(column: str) -> str:
    """Cast an EPSG:4326 geometry column to geography for meter-based math."""
    return f"{column}::geography"


def distance_m_sql(geom_a: str, geom_b: str) -> str:
    """ST_Distance(...) returning meters via geography cast."""
    return f"ST_Distance({geom_a}::geography, {geom_b}::geography)"


def distance_km_sql(geom_a: str, geom_b: str) -> str:
    """ST_Distance(...) in meters converted to kilometers."""
    return f"(ST_Distance({geom_a}::geography, {geom_b}::geography) / {METERS_PER_KM:g})"


def dwithin_sql(geom_a: str, geom_b: str, radius_meters) -> str:
    """ST_DWithin(...) with a meter radius via geography cast."""
    return f"ST_DWithin({geom_a}::geography, {geom_b}::geography, {_fmt(radius_meters)})"