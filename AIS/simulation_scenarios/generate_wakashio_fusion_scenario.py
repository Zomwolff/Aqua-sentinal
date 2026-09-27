#!/usr/bin/env python3
"""
Wakashio Oil Spill Simulation Scenario Generator
=================================================

Generates a scientifically accurate, historically realistic AIS trajectory CSV
for 10-15 vessels near Mauritius during the MV Wakashio oil spill event
(2020-08-09).

Historical Context:
- MV Wakashio grounded: 2020-07-25 at ~16:00 UTC
- Oil leak began: 2020-08-06 (first detection)
- Major leak: 2020-08-09 (peak spill, ~1000 tons)
- Location: 20°26.5'S, 57°44.7'E (approximately -20.442°, 57.745°)

Vessel Types and Realistic Speeds (based on maritime standards):
- Oil Tankers: 12-15 knots average (22-28 km/h)
- Container Ships: 18-25 knots (33-46 km/h)
- Bulk Carriers: 12-15 knots (22-28 km/h)
- Fishing Vessels: 8-12 knots (15-22 km/h)
- General Cargo: 12-18 knots (22-33 km/h)
- Tug Boats: 10-12 knots (19-22 km/h)

Simulation Details:
- Date: 2020-08-09 08:00 - 14:00 UTC (6 hours, compressed to 6 minutes at 60x)
- 1 culprit vessel (MT SUSPICIOUS) - exhibits clear anomalies, at spill site
- 2-3 suspicious vessels - moderate anomalies, nearby but not culprits
- 7-11 normal vessels - legitimate maritime traffic
- All trajectories based on actual shipping routes to/from Port Louis
"""

import csv
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple

# Wakashio spill epicenter (historically accurate coordinates)
SPILL_LAT = -20.442
SPILL_LON = 57.745

# Port Louis main harbor coordinates
PORT_LOUIS_LAT = -20.160
PORT_LOUIS_LON = 57.498

# Blue Bay Marine Park (near spill site)
BLUE_BAY_LAT = -20.450
BLUE_BAY_LON = 57.700

# Simulation parameters
START_TIME = datetime(2020, 8, 9, 8, 0, 0)  # 08:00 UTC
DURATION_HOURS = 6
INTERVAL_SECONDS = 120  # AIS report every 2 minutes (realistic for Class A)


def calculate_waypoint(start_lat: float, start_lon: float, 
                      bearing: float, distance_nm: float) -> Tuple[float, float]:
    """
    Calculate destination point given start point, bearing and distance.
    
    Args:
        start_lat: Starting latitude (decimal degrees)
        start_lon: Starting longitude (decimal degrees)
        bearing: Bearing in degrees (0-360)
        distance_nm: Distance in nautical miles
    
    Returns:
        Tuple of (latitude, longitude) in decimal degrees
    """
    # Convert to radians
    lat1 = math.radians(start_lat)
    lon1 = math.radians(start_lon)
    brng = math.radians(bearing)
    
    # Angular distance (in radians)
    # 1 nautical mile = 1/60 degree of latitude
    angular_dist = distance_nm / 60.0
    d = math.radians(angular_dist)
    
    # Calculate destination
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + 
                     math.cos(lat1) * math.sin(d) * math.cos(brng))
    
    lon2 = lon1 + math.atan2(math.sin(brng) * math.sin(d) * math.cos(lat1),
                              math.cos(d) - math.sin(lat1) * math.sin(lat2))
    
    return (math.degrees(lat2), math.degrees(lon2))


def generate_trajectory(waypoints: List[Tuple[float, float]], 
                       speed_knots: float,
                       start_time: datetime,
                       duration_hours: float) -> List[dict]:
    """
    Generate AIS position reports along a trajectory defined by waypoints.
    
    Args:
        waypoints: List of (lat, lon) tuples defining the route
        speed_knots: Vessel speed in knots
        start_time: Start datetime
        duration_hours: Total duration in hours
    
    Returns:
        List of position dicts with timestamp, lat, lon, speed, course, heading
    """
    positions = []
    current_time = start_time
    interval = timedelta(seconds=INTERVAL_SECONDS)
    end_time = start_time + timedelta(hours=duration_hours)
    
    # Calculate distances and bearings between waypoints
    segments = []
    for i in range(len(waypoints) - 1):
        lat1, lon1 = waypoints[i]
        lat2, lon2 = waypoints[i + 1]
        
        # Calculate bearing
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        dlon = math.radians(lon2 - lon1)
        
        y = math.sin(dlon) * math.cos(lat2_rad)
        x = (math.cos(lat1_rad) * math.sin(lat2_rad) - 
             math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon))
        bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
        
        # Calculate distance in nautical miles
        dlat = lat2 - lat1
        distance_nm = abs(dlat) * 60.0  # Approximate for small distances
        
        segments.append({
            'start': waypoints[i],
            'end': waypoints[i + 1],
            'bearing': bearing,
            'distance_nm': distance_nm
        })
    
    # Generate positions along the route
    segment_idx = 0
    segment_progress = 0.0
    
    while current_time <= end_time and segment_idx < len(segments):
        segment = segments[segment_idx]
        
        # Interpolate position along current segment
        lat1, lon1 = segment['start']
        lat2, lon2 = segment['end']
        
        lat = lat1 + (lat2 - lat1) * segment_progress
        lon = lon1 + (lon2 - lon1) * segment_progress
        
        # Add some realistic variation to speed (±10%)
        actual_speed = speed_knots * (1.0 + 0.1 * math.sin(current_time.timestamp() / 100.0))
        
        # Heading typically matches course, but can vary slightly
        heading = segment['bearing'] + 5 * math.sin(current_time.timestamp() / 50.0)
        heading = (heading + 360) % 360
        
        positions.append({
            'timestamp': current_time,
            'lat': round(lat, 6),
            'lon': round(lon, 6),
            'sog': round(actual_speed, 1),
            'cog': round(segment['bearing'], 1),
            'heading': round(heading, 1)
        })
        
        # Advance along segment
        distance_per_interval = speed_knots * (INTERVAL_SECONDS / 3600.0)  # nm
        segment_progress += distance_per_interval / max(segment['distance_nm'], 0.01)
        
        if segment_progress >= 1.0:
            segment_idx += 1
            segment_progress = 0.0
        
        current_time += interval
    
    return positions


def generate_culprit_vessel() -> List[dict]:
    """
    Generate MT SUSPICIOUS trajectory - the actual culprit vessel.
    
    Exhibits clear anomalous behavior:
    - Approaches spill site from southeast
    - 90-minute AIS gap near spill site (spoofing/shutdown)
    - Loitering at spill coordinates (0.5-2 knots for 30+ min)
    - Erratic course changes
    - Departs hastily toward open ocean
    """
    positions = []
    current_time = START_TIME
    
    # Phase 1: Normal approach from southeast (08:00 - 09:30)
    # Starting point: 30 nm southeast of spill
    start_lat, start_lon = calculate_waypoint(SPILL_LAT, SPILL_LON, 135, 30)
    waypoints_approach = [
        (start_lat, start_lon),
        calculate_waypoint(SPILL_LAT, SPILL_LON, 135, 15),
        calculate_waypoint(SPILL_LAT, SPILL_LON, 135, 5),
        (SPILL_LAT, SPILL_LON)
    ]
    
    positions.extend(generate_trajectory(
        waypoints_approach, 13.5, current_time, 1.5
    ))
    
    # Phase 2: AIS GAP (09:30 - 11:00) - NO POSITIONS EMITTED
    # This is the smoking gun - vessel goes dark near spill site
    gap_start = current_time + timedelta(hours=1, minutes=30)
    gap_end = gap_start + timedelta(hours=1, minutes=30)
    
    # Phase 3: Reappear at spill site, loitering (11:00 - 11:45)
    current_time = gap_end
    loiter_positions = []
    
    # Generate slow-moving positions in small circle around spill
    for i in range(25):  # 25 reports over 45 min
        angle = i * 14.4  # degrees
        radius_nm = 0.3  # 300 meters
        lat, lon = calculate_waypoint(SPILL_LAT, SPILL_LON, angle, radius_nm)
        
        loiter_positions.append({
            'timestamp': current_time,
            'lat': round(lat, 6),
            'lon': round(lon, 6),
            'sog': round(0.5 + 1.5 * math.sin(i / 5.0), 1),  # 0.5-2 knots
            'cog': round(angle, 1),
            'heading': round((angle + 20) % 360, 1)  # Heading doesn't match course
        })
        
        current_time += timedelta(minutes=2)
    
    positions.extend(loiter_positions)
    
    # Phase 4: Erratic departure (11:45 - 12:15)
    # Zig-zag pattern indicating evasive behavior
    depart_waypoints = [
        (SPILL_LAT, SPILL_LON),
        calculate_waypoint(SPILL_LAT, SPILL_LON, 45, 3),
        calculate_waypoint(SPILL_LAT, SPILL_LON, 90, 6),
        calculate_waypoint(SPILL_LAT, SPILL_LON, 60, 10)
    ]
    
    positions.extend(generate_trajectory(
        depart_waypoints, 16.5, current_time, 0.5
    ))
    
    # Phase 5: Rapid escape to open ocean (12:15 - 14:00)
    current_time = START_TIME + timedelta(hours=4, minutes=15)
    escape_waypoints = [
        calculate_waypoint(SPILL_LAT, SPILL_LON, 60, 10),
        calculate_waypoint(SPILL_LAT, SPILL_LON, 75, 25),
        calculate_waypoint(SPILL_LAT, SPILL_LON, 80, 45)
    ]
    
    positions.extend(generate_trajectory(
        escape_waypoints, 14.5, current_time, 1.75
    ))
    
    return positions


def generate_suspicious_vessel_1() -> List[dict]:
    """
    Generate DUBIOUS TRADER trajectory - moderately suspicious.
    
    Behavior:
    - Passes near spill site (5 nm away)
    - Brief slow-down near the area
    - Normal otherwise, but timing is suspicious
    """
    # Route: Port Louis to east, passing 5nm north of spill
    waypoints = [
        (PORT_LOUIS_LAT, PORT_LOUIS_LON),
        (-20.30, 57.60),
        (-20.35, 57.70),
        (-20.38, 57.85),  # Closest approach to spill (5 nm north)
        (-20.35, 58.00),
        (-20.30, 58.20)
    ]
    
    return generate_trajectory(waypoints, 14.5, START_TIME, DURATION_HOURS)


def generate_suspicious_vessel_2() -> List[dict]:
    """
    Generate FV QUESTIONABLE trajectory - moderately suspicious fishing vessel.
    
    Behavior:
    - Fishing pattern near spill area
    - Slow speeds, circular movements
    - Could be normal fishing OR observing spill
    """
    positions = []
    current_time = START_TIME
    
    # Circular fishing pattern 8 nm southwest of spill
    center_lat, center_lon = calculate_waypoint(SPILL_LAT, SPILL_LON, 225, 8)
    
    for i in range(180):  # Full 6 hours
        angle = (i * 2) % 360  # Complete circles
        radius_nm = 2.0 + 0.5 * math.sin(i / 20.0)  # Varying radius
        
        lat, lon = calculate_waypoint(center_lat, center_lon, angle, radius_nm)
        
        positions.append({
            'timestamp': current_time,
            'lat': round(lat, 6),
            'lon': round(lon, 6),
            'sog': round(6.5 + 2.5 * math.sin(i / 15.0), 1),  # 4-9 knots
            'cog': round(angle, 1),
            'heading': round((angle + 10) % 360, 1)
        })
        
        current_time += timedelta(minutes=2)
    
    return positions


def generate_normal_vessel(vessel_type: str, route_id: int) -> List[dict]:
    """
    Generate normal vessel traffic patterns for various routes around Mauritius.
    
    Routes:
    1. Port Louis to East (container)
    2. East to Port Louis (bulk carrier)
    3. North-South transit (general cargo)
    4. Fishing grounds (fishing vessel)
    5. Coastal patrol (government vessel)
    6. Tug operations near port (tug)
    """
    routes = {
        1: {  # Container ship: Port Louis → East
            'waypoints': [
                (PORT_LOUIS_LAT, PORT_LOUIS_LON),
                (-20.15, 57.65),
                (-20.18, 57.90),
                (-20.20, 58.20),
                (-20.18, 58.50)
            ],
            'speed': 22.0
        },
        2: {  # Bulk carrier: East → Port Louis
            'waypoints': [
                (-20.25, 58.80),
                (-20.23, 58.50),
                (-20.20, 58.15),
                (-20.17, 57.75),
                (-20.15, 57.55),
                (PORT_LOUIS_LAT, PORT_LOUIS_LON)
            ],
            'speed': 13.5
        },
        3: {  # General cargo: North → South transit
            'waypoints': [
                (-19.95, 57.60),
                (-20.10, 57.62),
                (-20.30, 57.65),
                (-20.50, 57.68),
                (-20.70, 57.70)
            ],
            'speed': 15.5
        },
        4: {  # Fishing vessel: offshore grounds
            'waypoints': [
                (-20.05, 57.85),
                (-20.12, 57.95),
                (-20.18, 58.05),
                (-20.22, 58.10),
                (-20.20, 58.20)
            ],
            'speed': 9.5
        },
        5: {  # Coastal patrol: South coast
            'waypoints': [
                (-20.48, 57.35),
                (-20.46, 57.50),
                (-20.47, 57.65),
                (-20.49, 57.80),
                (-20.48, 57.95)
            ],
            'speed': 18.0
        },
        6: {  # Tug: Port operations
            'waypoints': [
                (PORT_LOUIS_LAT, PORT_LOUIS_LON),
                (PORT_LOUIS_LAT + 0.02, PORT_LOUIS_LON + 0.01),
                (PORT_LOUIS_LAT + 0.01, PORT_LOUIS_LON + 0.03),
                (PORT_LOUIS_LAT - 0.01, PORT_LOUIS_LON + 0.02),
                (PORT_LOUIS_LAT, PORT_LOUIS_LON)
            ],
            'speed': 10.5
        },
        7: {  # Container: South → North
            'waypoints': [
                (-20.65, 57.55),
                (-20.45, 57.58),
                (-20.25, 57.60),
                (-20.05, 57.62),
                (-19.90, 57.65)
            ],
            'speed': 21.0
        },
        8: {  # Tanker: Transit route
            'waypoints': [
                (-19.85, 57.30),
                (-20.00, 57.40),
                (-20.15, 57.55),
                (-20.25, 57.70),
                (-20.32, 57.88)
            ],
            'speed': 13.0
        },
        9: {  # Fishing: Mahebourg Bay
            'waypoints': [
                (-20.40, 57.68),
                (-20.42, 57.72),
                (-20.45, 57.70),
                (-20.43, 57.67),
                (-20.41, 57.69)
            ],
            'speed': 7.5
        },
        10: {  # General cargo: circumnavigation
            'waypoints': [
                (-20.10, 57.75),
                (-20.25, 58.00),
                (-20.45, 58.10),
                (-20.55, 57.95),
                (-20.50, 57.70)
            ],
            'speed': 16.0
        }
    }
    
    route = routes.get(route_id, routes[1])
    return generate_trajectory(
        route['waypoints'], 
        route['speed'], 
        START_TIME, 
        DURATION_HOURS
    )


def generate_scenario_csv():
    """
    Generate complete CSV scenario with all vessels.
    
    Vessel breakdown:
    - 1 culprit (MT SUSPICIOUS)
    - 2 suspicious (DUBIOUS TRADER, FV QUESTIONABLE)
    - 10 normal vessels (various types)
    Total: 13 vessels
    """
    vessels = [
        {
            'mmsi': '999888001',
            'name': 'MT SUSPICIOUS',
            'imo': '9999001',
            'callsign': 'DEMO1',
            'vessel_type': 'Tanker',
            'is_target': True,
            'generator': generate_culprit_vessel
        },
        {
            'mmsi': '999888002',
            'name': 'DUBIOUS TRADER',
            'imo': '9999002',
            'callsign': 'DEMO2',
            'vessel_type': 'Cargo',
            'is_target': False,
            'generator': generate_suspicious_vessel_1
        },
        {
            'mmsi': '999888003',
            'name': 'FV QUESTIONABLE',
            'imo': '',
            'callsign': 'DEMO3',
            'vessel_type': 'Fishing',
            'is_target': False,
            'generator': generate_suspicious_vessel_2
        },
        # Normal vessels
        {
            'mmsi': '999888004',
            'name': 'MAERSK PACIFIC',
            'imo': '9999004',
            'callsign': 'DEMO4',
            'vessel_type': 'Cargo',
            'is_target': False,
            'route_id': 1
        },
        {
            'mmsi': '999888005',
            'name': 'BULK HARMONY',
            'imo': '9999005',
            'callsign': 'DEMO5',
            'vessel_type': 'Cargo',
            'is_target': False,
            'route_id': 2
        },
        {
            'mmsi': '999888006',
            'name': 'GENERAL TRADER',
            'imo': '9999006',
            'callsign': 'DEMO6',
            'vessel_type': 'Cargo',
            'is_target': False,
            'route_id': 3
        },
        {
            'mmsi': '999888007',
            'name': 'FV NEPTUNE',
            'imo': '',
            'callsign': 'DEMO7',
            'vessel_type': 'Fishing',
            'is_target': False,
            'route_id': 4
        },
        {
            'mmsi': '999888008',
            'name': 'COAST GUARD 1',
            'imo': '9999008',
            'callsign': 'DEMO8',
            'vessel_type': 'Other',
            'is_target': False,
            'route_id': 5
        },
        {
            'mmsi': '999888009',
            'name': 'TUG MAURITIUS',
            'imo': '',
            'callsign': 'DEMO9',
            'vessel_type': 'Tug',
            'is_target': False,
            'route_id': 6
        },
        {
            'mmsi': '999888010',
            'name': 'MSC HORIZON',
            'imo': '9999010',
            'callsign': 'DEMO10',
            'vessel_type': 'Cargo',
            'is_target': False,
            'route_id': 7
        },
        {
            'mmsi': '999888011',
            'name': 'OCEAN TANKER',
            'imo': '9999011',
            'callsign': 'DEMO11',
            'vessel_type': 'Tanker',
            'is_target': False,
            'route_id': 8
        },
        {
            'mmsi': '999888012',
            'name': 'FV BLUE MARLIN',
            'imo': '',
            'callsign': 'DEMO12',
            'vessel_type': 'Fishing',
            'is_target': False,
            'route_id': 9
        },
        {
            'mmsi': '999888013',
            'name': 'ISLAND TRADER',
            'imo': '9999013',
            'callsign': 'DEMO13',
            'vessel_type': 'Cargo',
            'is_target': False,
            'route_id': 10
        }
    ]
    
    # Generate all position records
    all_records = []
    
    for vessel in vessels:
        print(f"Generating trajectory for {vessel['name']} (MMSI {vessel['mmsi']})...")
        
        if 'generator' in vessel:
            positions = vessel['generator']()
        else:
            positions = generate_normal_vessel(vessel['vessel_type'], vessel['route_id'])
        
        for pos in positions:
            all_records.append({
                'MMSI': vessel['mmsi'],
                'BaseDateTime': pos['timestamp'].strftime('%Y-%m-%dT%H:%M:%S'),
                'LAT': pos['lat'],
                'LON': pos['lon'],
                'SOG': pos['sog'],
                'COG': pos['cog'],
                'Heading': pos['heading'],
                'VesselName': vessel['name'],
                'IMO': vessel['imo'],
                'CallSign': vessel['callsign'],
                'VesselType': vessel['vessel_type'],
                'Status': 'under way using engine' if pos['sog'] > 3.0 else 'moored',
                'Length': '180' if vessel['vessel_type'] == 'Tanker' else ('120' if vessel['vessel_type'] == 'Cargo' else '25'),
                'Width': '32' if vessel['vessel_type'] == 'Tanker' else ('20' if vessel['vessel_type'] == 'Cargo' else '8'),
                'Draft': '12.5' if vessel['vessel_type'] == 'Tanker' else '8.0',
                'Cargo': '',
                'TransceiverClass': 'A',
                'IsTarget': 'true' if vessel['is_target'] else 'false'
            })
    
    # Sort by timestamp
    all_records.sort(key=lambda x: x['BaseDateTime'])
    
    # Write CSV
    output_path = Path(__file__).parent / 'wakashio_fusion_demo.csv'
    
    with open(output_path, 'w', newline='') as f:
        fieldnames = ['MMSI', 'BaseDateTime', 'LAT', 'LON', 'SOG', 'COG', 'Heading',
                     'VesselName', 'IMO', 'CallSign', 'VesselType', 'Status',
                     'Length', 'Width', 'Draft', 'Cargo', 'TransceiverClass', 'IsTarget']
        
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_records)
    
    print(f"\n✅ Generated {len(all_records)} AIS records for {len(vessels)} vessels")
    print(f"📍 Spill location: {SPILL_LAT}, {SPILL_LON}")
    print(f"⏱️  Time range: {START_TIME} to {START_TIME + timedelta(hours=DURATION_HOURS)}")
    print(f"💾 Saved to: {output_path}")
    
    # Statistics
    culprit_records = [r for r in all_records if r['IsTarget'] == 'true']
    print(f"\n📊 Statistics:")
    print(f"   - Total vessels: {len(vessels)}")
    print(f"   - Culprit vessel (MT SUSPICIOUS): {len(culprit_records)} positions")
    print(f"   - AIS gap: ~90 minutes (09:30-11:00 UTC)")
    print(f"   - Loitering at spill site: 11:00-11:45 UTC")
    print(f"   - Compression ratio: 60x (6 hours → 6 minutes)")
    

if __name__ == '__main__':
    print("=" * 70)
    print("Wakashio Oil Spill Simulation Scenario Generator")
    print("Historically Accurate AIS Data for 2020-08-09")
    print("=" * 70)
    print()
    
    generate_scenario_csv()
    
    print("\n✅ Scenario generation complete!")
    print("\nNext steps:")
    print("1. Review the CSV file for accuracy")
    print("2. Visualize trajectories (optional)")
    print("3. Use with the spill simulator service")
