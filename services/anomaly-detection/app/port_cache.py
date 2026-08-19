"""Minimal port proximity cache for anomaly detection service."""
from typing import Optional

_MUMBAI_PORTS = [
    {"name": "Mumbai Port",        "lat": 18.936, "lon": 72.838, "radius_m": 5000},
    {"name": "Nhava Sheva (JNPT)", "lat": 18.948, "lon": 72.952, "radius_m": 4000},
    {"name": "Kandla Port",        "lat": 23.003, "lon": 70.215, "radius_m": 3000},
    {"name": "Mundra Port",        "lat": 22.769, "lon": 69.710, "radius_m": 3000},
    {"name": "Mormugao Port",      "lat": 15.412, "lon": 73.798, "radius_m": 2000},
    {"name": "New Mangalore Port", "lat": 12.921, "lon": 74.816, "radius_m": 2000},
]


def is_near_port(lat: float, lon: float, radius_m: float = 2000) -> Optional[str]:
    from shared.geo_utils import haversine_distance
    for p in _MUMBAI_PORTS:
        if haversine_distance(lat, lon, p["lat"], p["lon"]) <= max(radius_m, p["radius_m"]):
            return p["name"]
    return None
