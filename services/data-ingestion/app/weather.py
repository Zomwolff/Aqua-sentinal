import asyncio
import logging
import httpx
from datetime import datetime, timezone
import os
import sys

sys.path.insert(0, "/app")
from shared.db.connection import get_pool

log = logging.getLogger(__name__)

# Mumbai / Arabian Sea AOI for environmental forecast (oil spill drift modeling)
BBOX_MIN_LAT = float(os.environ.get("WEATHER_BBOX_MIN_LAT", "14.0"))
BBOX_MAX_LAT = float(os.environ.get("WEATHER_BBOX_MAX_LAT", "25.0"))
BBOX_MIN_LON = float(os.environ.get("WEATHER_BBOX_MIN_LON", "68.0"))
BBOX_MAX_LON = float(os.environ.get("WEATHER_BBOX_MAX_LON", "77.5"))

async def fetch_and_store_weather():
    """Fetch hourly wind and ocean current forecast from Open-Meteo and store in DB."""
    # Generate 6×5 grid for better spatial coverage (50 km Oil Spread lookup radius)
    lat_steps = 6
    lon_steps = 5
    lat_spacing = (BBOX_MAX_LAT - BBOX_MIN_LAT) / (lat_steps - 1)
    lon_spacing = (BBOX_MAX_LON - BBOX_MIN_LON) / (lon_steps - 1)
    
    grid_lats, grid_lons = [], []
    for i in range(lat_steps):
        for j in range(lon_steps):
            lat = BBOX_MIN_LAT + i * lat_spacing
            lon = BBOX_MIN_LON + j * lon_spacing
            grid_lats.append(str(round(lat, 4)))
            grid_lons.append(str(round(lon, 4)))

    lat_str = ",".join(grid_lats)
    lon_str = ",".join(grid_lons)

    try:
        # Fetch 30-hour hourly forecast (acquisition_time ±3h to +27h window for Oil Spread V2)
        wind_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat_str}&longitude={lon_str}&hourly=wind_speed_10m,wind_direction_10m&forecast_hours=30&wind_speed_unit=kmh"
        marine_url = f"https://marine-api.open-meteo.com/v1/marine?latitude={lat_str}&longitude={lon_str}&hourly=ocean_current_velocity,ocean_current_direction&forecast_hours=30"

        async with httpx.AsyncClient(timeout=30.0) as client:
            wind_resp, marine_resp = await asyncio.gather(
                client.get(wind_url),
                client.get(marine_url),
                return_exceptions=True
            )

        if isinstance(wind_resp, Exception):
            log.error("Failed to fetch wind data: %s", wind_resp)
            return
        if isinstance(marine_resp, Exception):
            log.error("Failed to fetch marine data: %s", marine_resp)
            return

        if wind_resp.status_code != 200:
            log.error("Wind API returned %s: %s", wind_resp.status_code, wind_resp.text[:200])
            return
        if marine_resp.status_code != 200:
            log.error("Marine API returned %s: %s", marine_resp.status_code, marine_resp.text[:200])
            return

        wind_results = wind_resp.json()
        marine_results = marine_resp.json()
        if not isinstance(wind_results, list):
            wind_results = [wind_results]
        if not isinstance(marine_results, list):
            marine_results = [marine_results]

        pool = await get_pool()
        
        # Prune old data (keep only records from last 36 hours)
        try:
            deleted = await pool.execute(
                "DELETE FROM environmental_conditions WHERE timestamp < NOW() - INTERVAL '36 hours'"
            )
            log.debug("Pruned old environmental data: %s", deleted)
        except Exception as e:
            log.warning("Failed to prune old environmental data: %s", e)

        inserted_count = 0
        for w_res, m_res in zip(wind_results, marine_results):
            lat = w_res.get("latitude")
            lon = w_res.get("longitude")
            w_hourly = w_res.get("hourly", {})
            m_hourly = m_res.get("hourly", {})
            
            # Parse hourly arrays
            timestamps = w_hourly.get("time", [])
            wind_speeds = w_hourly.get("wind_speed_10m", [])
            wind_directions = w_hourly.get("wind_direction_10m", [])
            current_velocities = m_hourly.get("ocean_current_velocity", [])
            current_directions = m_hourly.get("ocean_current_direction", [])
            
            if not timestamps:
                log.warning("No hourly timestamps for grid point (%s, %s)", lat, lon)
                continue
            
            # Insert one row per forecast hour
            for idx, ts_str in enumerate(timestamps):
                try:
                    # Parse ISO timestamp
                    ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    
                    wind_speed_kmh = wind_speeds[idx] if idx < len(wind_speeds) else None
                    wind_direction_deg = wind_directions[idx] if idx < len(wind_directions) else None
                    curr_vel_kmh = current_velocities[idx] if idx < len(current_velocities) else None
                    current_speed_ms = (curr_vel_kmh / 3.6) if curr_vel_kmh is not None else None
                    current_direction_deg = current_directions[idx] if idx < len(current_directions) else None
                    
                    await pool.execute(
                        """
                        INSERT INTO environmental_conditions
                        (timestamp, latitude, longitude, wind_speed_kmh, wind_direction_deg, current_speed_ms, current_direction_deg, source)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, 'open_meteo')
                        """,
                        ts, lat, lon, wind_speed_kmh, wind_direction_deg, current_speed_ms, current_direction_deg
                    )
                    inserted_count += 1
                except Exception as e:
                    log.warning("Failed to insert environmental record for (%s, %s) at %s: %s", lat, lon, ts_str, e)
                    continue
        
        log.info("Successfully ingested environmental forecast data: %d grid points, %d total records inserted.", 
                 len(wind_results), inserted_count)
        
    except Exception as e:
        log.exception("Error in fetch_and_store_weather: %s", e)

async def weather_polling_loop():
    """Background task to fetch weather forecast every 6 hours."""
    log.info("Starting weather polling loop (6-hour interval for forecast data)...")
    while True:
        await fetch_and_store_weather()
        await asyncio.sleep(21600)  # 6 hours
