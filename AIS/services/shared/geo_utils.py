"""Geospatial utility functions for maritime calculations."""

import math
from typing import Tuple


def bearing_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate bearing (direction) from point (lat1, lon1) to point (lat2, lon2).
    
    Args:
        lat1, lon1: Starting latitude, longitude (degrees)
        lat2, lon2: Ending latitude, longitude (degrees)
    
    Returns:
        Bearing in degrees (0-360), where 0/360 is North, 90 is East, etc.
    """
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    
    x = math.sin(dlon) * math.cos(lat2_rad)
    y = (math.cos(lat1_rad) * math.sin(lat2_rad) -
         math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon))
    
    bearing_rad = math.atan2(x, y)
    bearing_deg = math.degrees(bearing_rad)
    
    # Normalize to 0-360
    bearing_deg = (bearing_deg + 360) % 360
    
    return bearing_deg


def distance_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate great-circle distance between two points (Haversine formula).
    
    Args:
        lat1, lon1: Starting latitude, longitude (degrees)
        lat2, lon2: Ending latitude, longitude (degrees)
    
    Returns:
        Distance in meters
    """
    R = 6371000  # Earth's radius in meters
    
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    
    return R * c


def is_within_radius(
    lat1: float, lon1: float, lat2: float, lon2: float, radius_m: float
) -> bool:
    """Check if point (lat2, lon2) is within radius_m of (lat1, lon1)."""
    dist = distance_between(lat1, lon1, lat2, lon2)
    return dist <= radius_m
