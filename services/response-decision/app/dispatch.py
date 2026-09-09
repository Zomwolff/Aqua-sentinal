"""Find certified response vessels from the existing AIS vessel cache."""
from __future__ import annotations

from typing import Any, Dict, List

try:
    from shared.geo_utils import haversine_distance
except ModuleNotFoundError:
    from services.shared.geo_utils import haversine_distance


async def find_nearest_certified_vessels(
    pool: Any,
    spill_lat: float,
    spill_lon: float,
    limit: int = 5,
    max_distance_m: float = 100_000.0,
) -> List[Dict[str, Any]]:
    """Match certified vessels to fresh AIS positions near a spill."""
    rows = await pool.fetch(
        """
        SELECT cv.mmsi, cv.tier_rating, cv.equipment_summary,
               c.name AS contractor_name, c.phone, c.email,
               v.name AS vessel_name, v.last_lat, v.last_lon, v.last_seen
        FROM certified_vessels cv
        JOIN contractors c ON c.id = cv.contractor_id AND c.active
        JOIN vessels v ON v.mmsi = cv.mmsi
        WHERE v.last_lat IS NOT NULL AND v.last_lon IS NOT NULL
          AND v.last_seen IS NOT NULL
        """
    )
    matches = []
    for row in rows:
        distance_m = haversine_distance(
            float(spill_lat), float(spill_lon),
            float(row["last_lat"]), float(row["last_lon"]),
        )
        if distance_m <= max_distance_m:
            matches.append({
                "mmsi": str(row["mmsi"]),
                "vessel_name": row["vessel_name"],
                "contractor_name": row["contractor_name"],
                "tier_rating": row["tier_rating"],
                "equipment_summary": row["equipment_summary"],
                "phone": row["phone"],
                "email": row["email"],
                "latitude": float(row["last_lat"]),
                "longitude": float(row["last_lon"]),
                "last_seen": row["last_seen"].isoformat(),
                "distance_m": round(distance_m, 1),
            })
    matches.sort(key=lambda item: item["distance_m"])
    return matches[:max(0, int(limit))]
