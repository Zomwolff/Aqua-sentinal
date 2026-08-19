"""
Loads static geospatial reference layers (coastlines, ports, protected zones)
from GeoJSON files into the PostGIS reference_layers table.

Also provides fast in-memory lookups that downstream logic can use without
querying the database on every AIS ping.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# In-memory caches for fast lookup (populated at startup)
_PORT_POLYGONS: List[Dict[str, Any]] = []  # [{name, coords, center_lat, center_lon}]
_PROTECTED_ZONES: List[Dict[str, Any]] = []
_COASTLINE_SEGMENTS: List[Tuple[float, float]] = []  # list of (lat, lon) points


def _geo_layers_dir() -> Path:
    return Path("/data/geo_layers")


def _load_geojson_file(path: Path) -> Optional[Dict[str, Any]]:
    """Load a GeoJSON file and return the parsed dict, or None on error."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        log.debug("GeoJSON file not found: %s", path)
        return None
    except json.JSONDecodeError as e:
        log.warning("Invalid GeoJSON at %s: %s", path, e)
        return None


def _extract_polygon_center(coords: List) -> Tuple[float, float]:
    """Compute the centroid of a simple polygon ring (list of [lon, lat] pairs)."""
    if not coords:
        return 0.0, 0.0
    lons = [c[0] for c in coords if len(c) >= 2]
    lats = [c[1] for c in coords if len(c) >= 2]
    return sum(lats) / len(lats), sum(lons) / len(lons)


async def load_reference_layers(pool) -> None:
    """
    Load all GeoJSON files from /data/geo_layers/ into:
    1. The PostGIS reference_layers table (for SQL-based spatial queries)
    2. In-memory caches (for fast Python-side lookups in the hot path)

    Called once at service startup. Safe to call multiple times (idempotent —
    uses ON CONFLICT DO NOTHING on the DB side).

    If /data/geo_layers/ doesn't exist or is empty, logs a warning and loads
    built-in fallbacks for the Mumbai offshore area.
    """
    geo_dir = _geo_layers_dir()

    if not geo_dir.exists():
        log.warning(
            "/data/geo_layers/ not found. Using built-in Mumbai offshore fallbacks."
            " For production, mount GeoJSON files at /data/geo_layers/"
        )
        _load_mumbai_fallbacks()
        return

    loaded_count = 0
    for geojson_path in sorted(geo_dir.glob("*.geojson")) + sorted(geo_dir.glob("*.json")):
        geojson = _load_geojson_file(geojson_path)
        if geojson is None:
            continue

        layer_type = _infer_layer_type(geojson_path.stem)
        features = geojson.get("features", [])
        if not features and geojson.get("type") == "Feature":
            features = [geojson]

        for feature in features:
            geom = feature.get("geometry", {})
            props = feature.get("properties", {})
            name = props.get("name") or props.get("NAME") or geojson_path.stem

            if geom.get("type") == "Point":
                lon, lat = geom["coordinates"][:2]
                wkt = f"POINT({lon} {lat})"
                _cache_point(layer_type, name, lat, lon)
            elif geom.get("type") == "Polygon":
                ring = geom["coordinates"][0]
                coords_wkt = ", ".join(f"{c[0]} {c[1]}" for c in ring)
                wkt = f"POLYGON(({coords_wkt}))"
                center_lat, center_lon = _extract_polygon_center(ring)
                _cache_polygon(layer_type, name, ring, center_lat, center_lon)
            elif geom.get("type") == "MultiPolygon":
                # Store each sub-polygon separately
                for poly in geom["coordinates"]:
                    ring = poly[0]
                    coords_wkt = ", ".join(f"{c[0]} {c[1]}" for c in ring)
                    wkt = f"POLYGON(({coords_wkt}))"
                    center_lat, center_lon = _extract_polygon_center(ring)
                    _cache_polygon(layer_type, name, ring, center_lat, center_lon)
            else:
                continue

            # Persist to PostGIS
            try:
                await pool.execute(
                    "INSERT INTO reference_layers (layer_type, name, geom) "
                    "VALUES ($1, $2, ST_GeomFromText($3, 4326)::geography) "
                    "ON CONFLICT DO NOTHING",
                    layer_type, name, wkt,
                )
                loaded_count += 1
            except Exception as e:
                log.warning("Failed to insert reference layer '%s': %s", name, e)

    if loaded_count == 0:
        log.warning("No reference layers loaded from /data/geo_layers/. Using built-in Mumbai fallbacks.")
        _load_mumbai_fallbacks()
    else:
        log.info("Loaded %d reference layer features from /data/geo_layers/", loaded_count)


def _infer_layer_type(filename_stem: str) -> str:
    """Guess layer_type from filename."""
    s = filename_stem.lower()
    if "port" in s or "harbour" in s or "harbor" in s:
        return "port"
    if "coast" in s or "shore" in s:
        return "coastline"
    if "protect" in s or "marine" in s or "sanctuary" in s:
        return "protected_zone"
    if "anchor" in s:
        return "anchorage"
    if "eez" in s or "exclusive" in s:
        return "eez"
    return "other"


def _cache_point(layer_type: str, name: str, lat: float, lon: float) -> None:
    if layer_type in ("port", "anchorage"):
        _PORT_POLYGONS.append({"name": name, "center_lat": lat, "center_lon": lon, "radius_m": 2000})


def _cache_polygon(layer_type: str, name: str, ring: List, center_lat: float, center_lon: float) -> None:
    entry = {"name": name, "coords": ring, "center_lat": center_lat, "center_lon": center_lon}
    if layer_type in ("port", "anchorage"):
        _PORT_POLYGONS.append(entry)
    elif layer_type in ("protected_zone",):
        _PROTECTED_ZONES.append(entry)
    elif layer_type == "coastline":
        for coord in ring:
            _COASTLINE_SEGMENTS.append((coord[1], coord[0]))  # lat, lon


def _load_mumbai_fallbacks() -> None:
    """
    Built-in approximate port positions for the Mumbai offshore area.
    Used when no GeoJSON files are present.
    These are approximate centroids, not precise polygons.
    """
    global _PORT_POLYGONS
    _PORT_POLYGONS = [
        {"name": "Mumbai Port", "center_lat": 18.936, "center_lon": 72.838, "radius_m": 5000},
        {"name": "Nhava Sheva (JNPT)", "center_lat": 18.948, "center_lon": 72.952, "radius_m": 4000},
        {"name": "Kandla Port", "center_lat": 23.003, "center_lon": 70.215, "radius_m": 3000},
        {"name": "Mundra Port", "center_lat": 22.769, "center_lon": 69.710, "radius_m": 3000},
        {"name": "Goa Mormugao Port", "center_lat": 15.412, "center_lon": 73.798, "radius_m": 2000},
        {"name": "New Mangalore Port", "center_lat": 12.921, "center_lon": 74.816, "radius_m": 2000},
    ]
    log.info("Loaded %d built-in Mumbai fallback port locations", len(_PORT_POLYGONS))


# ──────────────────────────────────────────────────────────────────────────────
# In-memory lookup API (called from analytics / anomaly detection)
# ──────────────────────────────────────────────────────────────────────────────

def get_port_polygons() -> List[Dict[str, Any]]:
    """Return the cached list of port/anchorage location dicts."""
    return _PORT_POLYGONS


def get_protected_zones() -> List[Dict[str, Any]]:
    """Return the cached list of protected zone dicts."""
    return _PROTECTED_ZONES


def is_near_port(lat: float, lon: float, radius_m: float = 2000) -> Optional[str]:
    """
    Return the name of the nearest port if within radius_m metres, else None.
    Uses a simple distance check against port centroids — fast enough for
    the per-ping call frequency.
    """
    from shared.geo_utils import haversine_distance
    for port in _PORT_POLYGONS:
        d = haversine_distance(lat, lon, port["center_lat"], port["center_lon"])
        port_radius = port.get("radius_m", radius_m)
        if d <= max(radius_m, port_radius):
            return port["name"]
    return None
