SRID = 4326
METERS_PER_KM = 1000.0

# Mumbai Harbour, JNPT, and the Arabian Sea approach channel — SIH260361
# Option 1's study area. Bounding-box placeholder pending the exact boundary
# from the official reference file, not a real coastline polygon.
MUMBAI_AOI_BOUNDS = (72.75, 18.85, 73.05, 19.15)  # (min_lon, min_lat, max_lon, max_lat)

# Sentinel-1 IW GRD ground-range resolution.
SENTINEL1_PIXEL_SIZE_M = 10.0


def km_to_m(km: float) -> float:
    return km * METERS_PER_KM


def m_to_km(meters: float) -> float:
    return meters / METERS_PER_KM