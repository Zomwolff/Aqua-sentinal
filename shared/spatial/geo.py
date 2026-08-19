from shared.spatial.constants import METERS_PER_KM, SRID


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