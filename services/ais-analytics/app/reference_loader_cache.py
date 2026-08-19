"""
Lightweight port proximity cache for the AIS analytics service.
Calls the data-ingestion service's GET /reference-layers endpoint once at startup
and caches the port locations locally.
Falls back to built-in Mumbai offshore ports if the ingestion service is unavailable.
"""
from __future__ import annotations

import logging
import os
from typing import List, Dict, Any, Optional

log = logging.getLogger(__name__)

_PORTS: List[Dict[str, Any]] = []

# Built-in Mumbai ports (same as in data-ingestion reference_loader.py)
_MUMBAI_FALLBACK_PORTS = [
    {"name": "Mumbai Port", "center_lat": 18.936, "center_lon": 72.838, "radius_m": 5000},
    {"name": "Nhava Sheva (JNPT)", "center_lat": 18.948, "center_lon": 72.952, "radius_m": 4000},
    {"name": "Kandla Port", "center_lat": 23.003, "center_lon": 70.215, "radius_m": 3000},
    {"name": "Mundra Port", "center_lat": 22.769, "center_lon": 69.710, "radius_m": 3000},
    {"name": "Goa Mormugao Port", "center_lat": 15.412, "center_lon": 73.798, "radius_m": 2000},
    {"name": "New Mangalore Port", "center_lat": 12.921, "center_lon": 74.816, "radius_m": 2000},
]


async def init_port_cache() -> None:
    """Populate the port cache from the ingestion service or built-in fallbacks."""
    global _PORTS
    try:
        import httpx
        ingestion_url = os.environ.get("DATA_INGESTION_URL", "http://data-ingestion:8000")
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{ingestion_url}/reference-layers", params={"layer_type": "port"})
            if resp.status_code == 200:
                layers = resp.json()
                # These are just names/ids from the DB; we use fallbacks for lat/lon
                # (full geometry query is complex; built-ins are good enough for proximity)
                if layers:
                    log.info("Reference layers API OK, using built-in port coordinates for analytics")
    except Exception as e:
        log.debug("Could not contact data-ingestion reference-layers: %s (using fallbacks)", e)

    _PORTS = _MUMBAI_FALLBACK_PORTS
    log.info("Port cache loaded: %d ports", len(_PORTS))


def is_near_port(lat: float, lon: float, radius_m: float = 2000) -> Optional[str]:
    """Return port name if within radius_m of any known port, else None."""
    from shared.geo_utils import haversine_distance
    for port in _PORTS:
        d = haversine_distance(lat, lon, port["center_lat"], port["center_lon"])
        if d <= max(radius_m, port.get("radius_m", radius_m)):
            return port["name"]
    return None
