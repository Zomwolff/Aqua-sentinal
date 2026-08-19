SRID = 4326
METERS_PER_KM = 1000.0


def km_to_m(km: float) -> float:
    return km * METERS_PER_KM


def m_to_km(meters: float) -> float:
    return meters / METERS_PER_KM