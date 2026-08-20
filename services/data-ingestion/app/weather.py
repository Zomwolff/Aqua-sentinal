import asyncio
import logging
import httpx
from datetime import datetime, timezone
import os
import sys

sys.path.insert(0, "/app")
from shared.db import get_pool

log = logging.getLogger(__name__)

# Center of Mumbai offshore AOI by default
BBOX_MIN_LAT = float(os.environ.get("AIS_BBOX_MIN_LAT", "14.0"))
BBOX_MAX_LAT = float(os.environ.get("AIS_BBOX_MAX_LAT", "25.0"))
BBOX_MIN_LON = float(os.environ.get("AIS_BBOX_MIN_LON", "68.0"))
BBOX_MAX_LON = float(os.environ.get("AIS_BBOX_MAX_LON", "77.5"))

async def fetch_and_store_weather():
    """Fetch current wind and ocean current data from Open-Meteo and store in DB."""
    lats = [BBOX_MIN_LAT, (BBOX_MIN_LAT + BBOX_MAX_LAT) / 2.0, BBOX_MAX_LAT]
    lons = [BBOX_MIN_LON, (BBOX_MIN_LON + BBOX_MAX_LON) / 2.0, BBOX_MAX_LON]
    grid_lats, grid_lons = [], []
    for lat in lats:
        for lon in lons:
            grid_lats.append(str(round(lat, 4)))
            grid_lons.append(str(round(lon, 4)))

    lat_str = ",".join(grid_lats)
    lon_str = ",".join(grid_lons)

    try:
        wind_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat_str}&longitude={lon_str}&current=wind_speed_10m,wind_direction_10m&wind_speed_unit=kmh"
        marine_url = f"https://marine-api.open-meteo.com/v1/marine?latitude={lat_str}&longitude={lon_str}&current=ocean_current_velocity,ocean_current_direction"

        async with httpx.AsyncClient(timeout=10.0) as client:
            wind_resp, marine_resp = await asyncio.gather(
                client.get(wind_url),
                client.get(marine_url),
                return_exceptions=True
            )

        if isinstance(wind_resp, Exception) or isinstance(marine_resp, Exception):
            log.error("Failed to fetch weather data: wind_resp=%s, marine_resp=%s", wind_resp, marine_resp)
            return

        if wind_resp.status_code != 200 or marine_resp.status_code != 200:
            log.error("Weather API returned non-200. Wind: %s, Marine: %s", wind_resp.status_code, marine_resp.status_code)
            return

        wind_results = wind_resp.json()
        marine_results = marine_resp.json()
        if not isinstance(wind_results, list):
            wind_results = [wind_results]
        if not isinstance(marine_results, list):
            marine_results = [marine_results]

        now = datetime.now(timezone.utc)
        pool = await get_pool()

        for w_res, m_res in zip(wind_results, marine_results):
            lat = w_res.get("latitude")
            lon = w_res.get("longitude")
            w_curr = w_res.get("current", {})
            m_curr = m_res.get("current", {})

            wind_speed_kmh = w_curr.get("wind_speed_10m")
            wind_direction_deg = w_curr.get("wind_direction_10m")
            curr_vel_kmh = m_curr.get("ocean_current_velocity")
            current_speed_ms = (curr_vel_kmh / 3.6) if curr_vel_kmh is not None else None
            current_direction_deg = m_curr.get("ocean_current_direction")

            await pool.execute(
                """
                INSERT INTO environmental_conditions
                (timestamp, latitude, longitude, wind_speed_kmh, wind_direction_deg, current_speed_ms, current_direction_deg, source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'open_meteo')
                """,
                now, lat, lon, wind_speed_kmh, wind_direction_deg, current_speed_ms, current_direction_deg
            )
        log.info("Successfully ingested bulk weather data for %d grid points.", len(wind_results))
        
    except Exception as e:
        log.exception("Error in fetch_and_store_weather: %s", e)

async def weather_polling_loop():
    """Background task to fetch weather every 15 minutes."""
    log.info("Starting weather polling loop...")
    while True:
        await fetch_and_store_weather()
        await asyncio.sleep(900)  # 15 minutes
