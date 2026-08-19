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

LAT = (BBOX_MIN_LAT + BBOX_MAX_LAT) / 2.0
LON = (BBOX_MIN_LON + BBOX_MAX_LON) / 2.0

async def fetch_and_store_weather():
    """Fetch current wind and ocean current data from Open-Meteo and store in DB."""
    try:
        # Wind data
        wind_url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&current=wind_speed_10m,wind_direction_10m&wind_speed_unit=kmh"
        # Marine data (currents)
        marine_url = f"https://marine-api.open-meteo.com/v1/marine?latitude={LAT}&longitude={LON}&current=ocean_current_velocity,ocean_current_direction"

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

        wind_data = wind_resp.json().get("current", {})
        marine_data = marine_resp.json().get("current", {})

        wind_speed_kmh = wind_data.get("wind_speed_10m")
        wind_direction_deg = wind_data.get("wind_direction_10m")
        # Open-Meteo marine returns ocean_current_velocity in km/h by default. Convert to m/s.
        curr_vel_kmh = marine_data.get("ocean_current_velocity")
        current_speed_ms = (curr_vel_kmh / 3.6) if curr_vel_kmh is not None else None
        current_direction_deg = marine_data.get("ocean_current_direction")

        now = datetime.now(timezone.utc)

        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO environmental_conditions
            (timestamp, latitude, longitude, wind_speed_kmh, wind_direction_deg, current_speed_ms, current_direction_deg, source)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'open_meteo')
            """,
            now, LAT, LON, wind_speed_kmh, wind_direction_deg, current_speed_ms, current_direction_deg
        )
        log.info("Successfully ingested weather data. Wind: %s km/h, Current: %s m/s", wind_speed_kmh, current_speed_ms)
        
    except Exception as e:
        log.exception("Error in fetch_and_store_weather: %s", e)

async def weather_polling_loop():
    """Background task to fetch weather every 15 minutes."""
    log.info("Starting weather polling loop...")
    while True:
        await fetch_and_store_weather()
        await asyncio.sleep(900)  # 15 minutes
