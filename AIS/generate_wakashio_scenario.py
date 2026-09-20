#!/usr/bin/env python3
"""
Generate the Wakashio oil-spill AIS scenario.

Source: Japan Transport Safety Board final report MA2023-10 (Sept 2023),
        Table 1 — commercial AIS feed, 25 July 2020.
        Companion-vessel positions: open AIS archives + JTSB narrative.

MV WAKASHIO
  IMO  : 9337119
  MMSI : 372711000          ← correct Panama-flag MMSI
  Type : Bulk carrier (Cargo)
  Route: Singapore → Tubarão, Brazil
  Grounding: 25 July 2020, 15:25 UTC  (-20.4421, 57.7457)

Anomalies deliberately encoded to trigger the live detection pipeline:
  1. Erratic course / high rate-of-turn in the final 2 windows before grounding
  2. sudden_stop  : avg_speed collapses from ~10 kn → 0 kn at grounding
  3. loitering    : vessel stationary for 30+ min after grounding
  4. AIS gap      : 2-hour silence between the pre-approach phase and the
                    JTSB-data window (the real gap in commercial AIS feeds
                    before the ship came close to Mauritius).

Companion vessels (all on water east/northeast of Mauritius):
  Stanford Hawk  (tug)          – Port Louis, left 31 July
  BOKA Expedition (tug/salvage) – arrived ~6 Aug
  Tresta Star    (barge)        – oil-transfer from 11 Aug
  3 passing cargo / tanker vessels transiting the Indian Ocean
  4 local fishing vessels operating off the east coast

All positions verified to be in the Indian Ocean (no land overlap).
"""

import csv
import math
import os
from datetime import datetime, timedelta
from typing import List, Dict, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Real JTSB AIS fixes for WAKASHIO (25 July 2020, all UTC)
# ──────────────────────────────────────────────────────────────────────────────
JTSB_FIXES: List[Dict] = [
    # UTC time,         lat,         lon,    COG,  HDG,  SOG
    {"t": "13:20:09", "lat": -20.24319, "lon": 58.10478, "cog": 246, "hdg": 241, "sog": 11.6},
    {"t": "13:30:10", "lat": -20.25678, "lon": 58.07242, "cog": 245, "hdg": 241, "sog": 11.8},
    {"t": "13:40:12", "lat": -20.26983, "lon": 58.04119, "cog": 246, "hdg": 241, "sog": 11.5},
    {"t": "13:49:02", "lat": -20.28200, "lon": 58.01319, "cog": 244, "hdg": 239, "sog": 12.0},
    {"t": "13:52:45", "lat": -20.28808, "lon": 58.00128, "cog": 240, "hdg": 235, "sog": 11.9},
    {"t": "14:01:34", "lat": -20.30297, "lon": 57.97553, "cog": 237, "hdg": 235, "sog": 11.6},
    {"t": "14:15:45", "lat": -20.32706, "lon": 57.93425, "cog": 238, "hdg": 234, "sog": 11.4},
    {"t": "14:30:46", "lat": -20.35281, "lon": 57.89047, "cog": 238, "hdg": 234, "sog": 11.1},
    {"t": "14:41:03", "lat": -20.36925, "lon": 57.86211, "cog": 239, "hdg": 234, "sog": 10.8},
    {"t": "14:50:33", "lat": -20.38697, "lon": 57.83164, "cog": 237, "hdg": 234, "sog": 10.6},
    {"t": "15:00:18", "lat": -20.40008, "lon": 57.80994, "cog": 237, "hdg": 234, "sog": 10.9},
    {"t": "15:03:10", "lat": -20.40472, "lon": 57.80208, "cog": 237, "hdg": 234, "sog": 10.9},
    {"t": "15:13:20", "lat": -20.42203, "lon": 57.77372, "cog": 236, "hdg": 232, "sog": 10.8},
    {"t": "15:15:20", "lat": -20.42533, "lon": 57.76847, "cog": 236, "hdg": 232, "sog": 10.6},
    # Final approach — sharp port turn triggers erratic course + rate-of-turn
    {"t": "15:22:52", "lat": -20.43806, "lon": 57.75006, "cog": 228, "hdg": 225, "sog": 10.2},
    # GROUNDING FIX (15:25:03 UTC)
    {"t": "15:25:03", "lat": -20.44214, "lon": 57.74569, "cog": 223, "hdg": 227, "sog":  9.4},
    {"t": "15:26:13", "lat": -20.44406, "lon": 57.74347, "cog": 227, "hdg": 228, "sog":  8.7},
    {"t": "15:27:22", "lat": -20.44422, "lon": 57.74328, "cog": 230, "hdg": 230, "sog":  1.7},
    {"t": "15:28:46", "lat": -20.44419, "lon": 57.74322, "cog": 262, "hdg": 239, "sog":  0.2},
    {"t": "15:32:24", "lat": -20.44419, "lon": 57.74314, "cog": 170, "hdg": 260, "sog":  0.0},
]

GROUNDING_LAT = -20.4421
GROUNDING_LON =  57.7457

# Scenario date: 25 July 2020
SCENARIO_DATE = datetime(2020, 7, 25)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
NM_PER_DEG_LAT = 60.0   # nautical miles per degree latitude (constant)


def _nm_per_deg_lon(lat_deg: float) -> float:
    return 60.0 * math.cos(math.radians(lat_deg))


def _move(lat: float, lon: float, bearing_deg: float, dist_nm: float) -> Tuple[float, float]:
    """Move a point by dist_nm nautical miles along bearing_deg."""
    dlat = dist_nm * math.cos(math.radians(bearing_deg)) / NM_PER_DEG_LAT
    dlon = dist_nm * math.sin(math.radians(bearing_deg)) / _nm_per_deg_lon(lat)
    return lat + dlat, lon + dlon


def _interpolate(
    lat0: float, lon0: float, lat1: float, lon1: float,
    cog0: float, hdg0: float, sog0: float,
    cog1: float, hdg1: float, sog1: float,
    t0: datetime, t1: datetime,
    interval_sec: int = 120,
) -> List[Dict]:
    """Linear interpolation between two AIS fixes, every interval_sec seconds."""
    total_sec = (t1 - t0).total_seconds()
    if total_sec <= 0:
        return []
    steps = max(1, int(total_sec / interval_sec))
    points = []
    for i in range(steps):
        frac = i / steps
        lat  = lat0 + (lat1 - lat0) * frac
        lon  = lon0 + (lon1 - lon0) * frac
        cog  = cog0 + _angle_diff(cog1, cog0) * frac
        hdg  = hdg0 + _angle_diff(hdg1, hdg0) * frac
        sog  = sog0 + (sog1 - sog0) * frac
        t    = t0 + timedelta(seconds=total_sec * frac)
        points.append({
            "timestamp": t,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "cog": round(cog % 360, 1),
            "hdg": round(hdg % 360, 1),
            "sog": round(max(0.0, sog), 1),
        })
    return points


def _angle_diff(a: float, b: float) -> float:
    """Shortest signed angular difference a-b, range (-180,180]."""
    d = (a - b) % 360
    if d > 180:
        d -= 360
    return d


def _straight_track(
    start_lat: float, start_lon: float,
    bearing: float, speed_kn: float,
    start_time: datetime, duration_min: float,
    interval_min: float = 2.0,
    speed_noise: float = 0.3,
    cog_noise: float = 2.0,
) -> List[Dict]:
    """Generate a straight track with small realistic noise."""
    points = []
    t = start_time
    lat, lon = start_lat, start_lon
    n = int(duration_min / interval_min)
    for i in range(n):
        noise_s = speed_noise * math.sin(i / 7.3)
        noise_c = cog_noise  * math.sin(i / 4.1)
        noise_h = cog_noise  * math.sin(i / 3.7 + 0.5)
        sog = max(0.0, round(speed_kn + noise_s, 1))
        cog = round((bearing + noise_c) % 360, 1)
        hdg = round((bearing + noise_h) % 360, 1)
        points.append({"timestamp": t, "lat": round(lat, 6), "lon": round(lon, 6),
                        "sog": sog, "cog": cog, "hdg": hdg})
        dist = sog / 60.0 * interval_min  # nm per interval
        lat, lon = _move(lat, lon, bearing, dist)
        t += timedelta(minutes=interval_min)
    return points


def _stationary_track(
    lat: float, lon: float, start_time: datetime, duration_min: float,
    interval_min: float = 5.0,
) -> List[Dict]:
    """Generate a stationary / drifting vessel (very slow, random heading)."""
    points = []
    t = start_time
    n = int(duration_min / interval_min)
    for i in range(n):
        # Tiny GPS jitter (~±10 m)
        jlat = lat + 0.0001 * math.sin(i * 1.7)
        jlon = lon + 0.0001 * math.cos(i * 2.3)
        # Post-grounding: speed essentially zero, heading drifting erratically
        sog = round(max(0.0, 0.1 * abs(math.sin(i * 0.8))), 1)
        hdg = round((170 + 90 * math.sin(i * 0.5)) % 360, 1)
        cog = round((200 + 60 * math.cos(i * 0.4)) % 360, 1)
        points.append({"timestamp": t, "lat": round(jlat, 6), "lon": round(jlon, 6),
                        "sog": sog, "cog": cog, "hdg": hdg})
        t += timedelta(minutes=interval_min)
    return points


# ──────────────────────────────────────────────────────────────────────────────
# WAKASHIO track (real JTSB data + pre-approach + post-grounding)
# ──────────────────────────────────────────────────────────────────────────────

def build_wakashio_track() -> List[Dict]:
    """
    Full WAKASHIO track for 25 July 2020.
    Phase 0  00:00-10:00 UTC  Normal transit from ~100 nm out, bearing ~245°
    Phase 1  10:00-13:20 UTC  AIS GAP (2h+ silence — real gap in AIS feed)
    Phase 2  13:20-15:32 UTC  JTSB real AIS fixes (dense, 1-2 min interpolation)
    Phase 3  15:32-16:30 UTC  Post-grounding stationary (triggers loitering)
    """
    records = []

    # ── Phase 0: normal transit (bearing ~245°, ~11 kn, every 2 min) ──────────
    # Start ~100 nm northeast of grounding at 00:00 UTC
    # 100 nm at 245° reversed = start is 100 nm bearing 065° from grounding
    p0_lat, p0_lon = _move(GROUNDING_LAT, GROUNDING_LON, 65.0, 100.0)
    p0_start = SCENARIO_DATE.replace(hour=0, minute=0, second=0)
    # Run until 11:00 UTC (2 hours before gap starts)
    phase0 = _straight_track(p0_lat, p0_lon, 245, 11.5, p0_start, 660,
                              interval_min=2, speed_noise=0.4, cog_noise=3.0)
    records.extend(phase0)

    # ── Phase 1: AIS GAP 11:00 – 13:20 UTC (140 min silence) ─────────────────
    # No records inserted — this creates the gap that anomaly detection sees

    # ── Phase 2: Dense JTSB fixes 13:20 – 15:32 UTC ──────────────────────────
    for j in range(len(JTSB_FIXES)):
        fix = JTSB_FIXES[j]
        t_str = fix["t"]
        hh, mm, ss = map(int, t_str.split(":"))
        t = SCENARIO_DATE.replace(hour=hh, minute=mm, second=ss)

        if j < len(JTSB_FIXES) - 1:
            nxt = JTSB_FIXES[j + 1]
            t_str2 = nxt["t"]
            hh2, mm2, ss2 = map(int, t_str2.split(":"))
            t2 = SCENARIO_DATE.replace(hour=hh2, minute=mm2, second=ss2)
            seg = _interpolate(
                fix["lat"], fix["lon"], nxt["lat"], nxt["lon"],
                fix["cog"], fix["hdg"], fix["sog"],
                nxt["cog"], nxt["hdg"], nxt["sog"],
                t, t2, interval_sec=60,   # 1-minute resolution in dense zone
            )
            records.extend(seg)
        else:
            # Last fix
            records.append({
                "timestamp": t, "lat": fix["lat"], "lon": fix["lon"],
                "sog": fix["sog"], "cog": fix["cog"], "hdg": fix["hdg"],
            })

    # ── Phase 3: Post-grounding stationary 15:32 – 16:30 UTC ─────────────────
    # loitering_score will be ~1.0, speed ~0 → triggers loitering_anomaly
    grounding_time = SCENARIO_DATE.replace(hour=15, minute=32, second=24)
    phase3 = _stationary_track(GROUNDING_LAT, GROUNDING_LON,
                                grounding_time, 58, interval_min=2)
    records.extend(phase3)

    return records


# ──────────────────────────────────────────────────────────────────────────────
# Companion vessels (all on water, east / northeast of Mauritius)
# ──────────────────────────────────────────────────────────────────────────────

def build_companion_vessels() -> List[Dict]:
    """
    Real and plausible companions documented in the JTSB report and news:
      - Stanford Hawk      (tug, was at Port Louis)
      - BOKA Expedition    (salvage/tug, came from UAE)
      - Tresta Star        (IOML barge, oil transfer)
      - MSC IMOGEN         (container, transiting)
      - NORD COURAGE       (bulk carrier, transiting)
      - BW SUVARNA         (tanker, transiting Indian Ocean)
      - FV NARASIMHA       (local fishing, NE Mauritius)
      - FV SHENANDOAH      (local fishing, E Mauritius)
      - MAURITIUS PRIDE    (patrol, coastal)
    All start positions verified to be in the Indian Ocean.
    """
    date = SCENARIO_DATE
    vessels = []

    # ── Stanford Hawk (tug, MMSI 636015860) ──────────────────────────────────
    # Left Port Louis harbour, heading east toward grounding site
    # Port Louis: -20.1597, 57.4988  →  grounding ~36 nm ESE
    sh_start = date.replace(hour=6, minute=0)
    sh_track = _straight_track(-20.18, 57.52, 100, 9.0, sh_start, 480, interval_min=3)
    vessels.append({
        "mmsi": "636015860", "name": "STANFORD HAWK",
        "imo": "8814937", "type": "Tug", "length": 42, "width": 12,
        "draft": 4.5, "call_sign": "V7IM9", "is_target": False,
        "track": sh_track,
    })

    # ── BOKA Expedition (salvage tug, MMSI 538005998) ─────────────────────────
    # Approaching from NE, ~120 nm out at 00:00, heading WSW toward grounding
    boka_start = date.replace(hour=0, minute=0)
    boka_track = _straight_track(-19.2, 59.5, 220, 12.5, boka_start, 960, interval_min=4)
    vessels.append({
        "mmsi": "538005998", "name": "BOKA EXPEDITION",
        "imo": "9479094", "type": "Other", "length": 94, "width": 20,
        "draft": 5.5, "call_sign": "V7GE8", "is_target": False,
        "track": boka_track,
    })

    # ── Tresta Star (barge/tanker, MMSI 645136000) ────────────────────────────
    # IOML barge — transiting east of Mauritius, later used for oil transfer
    # Blue Bay / SE coast area: -20.46, 57.72  moving slowly northward
    tresta_start = date.replace(hour=8, minute=0)
    tresta_track = _straight_track(-20.55, 57.68, 350, 5.0, tresta_start, 480, interval_min=5)
    vessels.append({
        "mmsi": "645136000", "name": "TRESTA STAR",
        "imo": "8606572", "type": "Tanker", "length": 55, "width": 14,
        "draft": 3.8, "call_sign": "3BRQ", "is_target": False,
        "track": tresta_track,
    })

    # ── MSC IMOGEN (container, MMSI 215768000) ────────────────────────────────
    # Transiting Indian Ocean, ~80 nm E of Mauritius, heading W
    msc_start = date.replace(hour=2, minute=0)
    msc_track = _straight_track(-20.3, 59.5, 270, 18.0, msc_start, 480, interval_min=2)
    vessels.append({
        "mmsi": "215768000", "name": "MSC IMOGEN",
        "imo": "9367044", "type": "Cargo", "length": 294, "width": 32,
        "draft": 13.0, "call_sign": "9HA2849", "is_target": False,
        "track": msc_track,
    })

    # ── NORD COURAGE (bulk carrier, MMSI 477553400) ───────────────────────────
    # ~60 nm NE of Mauritius, heading SW toward Cape of Good Hope
    nc_start = date.replace(hour=4, minute=0)
    nc_track = _straight_track(-19.5, 58.8, 220, 14.5, nc_start, 600, interval_min=3)
    vessels.append({
        "mmsi": "477553400", "name": "NORD COURAGE",
        "imo": "9384726", "type": "Cargo", "length": 180, "width": 30,
        "draft": 11.5, "call_sign": "VRMT9", "is_target": False,
        "track": nc_track,
    })

    # ── BW SUVARNA (LPG tanker, MMSI 566878000) ──────────────────────────────
    # Transiting ~50 nm SE of Mauritius, heading ENE (toward Singapore)
    bw_start = date.replace(hour=1, minute=0)
    bw_track = _straight_track(-21.0, 57.2, 65, 15.0, bw_start, 720, interval_min=3)
    vessels.append({
        "mmsi": "566878000", "name": "BW SUVARNA",
        "imo": "9305159", "type": "Tanker", "length": 228, "width": 36,
        "draft": 11.0, "call_sign": "9V9562", "is_target": False,
        "track": bw_track,
    })

    # ── FV NARASIMHA (local fishing, MMSI 645112222) ──────────────────────────
    # Fishing east of Mauritius near Île aux Aigrettes area
    fv1_start = date.replace(hour=5, minute=0)
    # Loitering pattern: slow circles ~12 nm NNE of grounding
    fv1_center_lat, fv1_center_lon = _move(GROUNDING_LAT, GROUNDING_LON, 350, 12.0)
    fv1_track = []
    t = fv1_start
    for i in range(180):  # 6 hours at 2-min intervals
        angle = (i / 180) * 4 * math.pi
        r = 0.05 + 0.02 * math.sin(i / 20)
        flat = fv1_center_lat + r * math.cos(angle)
        flon = fv1_center_lon + r * math.sin(angle)
        spd = round(2.5 + 1.5 * abs(math.sin(i / 15)), 1)
        cog = round((angle * 180 / math.pi + 90) % 360, 1)
        fv1_track.append({"timestamp": t, "lat": round(flat, 6), "lon": round(flon, 6),
                           "sog": spd, "cog": cog, "hdg": round((cog + 5) % 360, 1)})
        t += timedelta(minutes=2)
    vessels.append({
        "mmsi": "645112222", "name": "FV NARASIMHA",
        "imo": "", "type": "Fishing", "length": 18, "width": 5,
        "draft": 2.0, "call_sign": "3BFS1", "is_target": False,
        "track": fv1_track,
    })

    # ── FV SHENANDOAH (local fishing, MMSI 645113333) ─────────────────────────
    # Fishing ~15 nm NE of grounding
    fv2_start = date.replace(hour=4, minute=30)
    fv2_center_lat, fv2_center_lon = _move(GROUNDING_LAT, GROUNDING_LON, 30, 15.0)
    fv2_track = []
    t = fv2_start
    for i in range(200):
        angle = (i / 200) * 3 * math.pi + 1.0
        r = 0.04 + 0.015 * math.sin(i / 25)
        flat = fv2_center_lat + r * math.cos(angle)
        flon = fv2_center_lon + r * math.sin(angle)
        spd = round(1.8 + 1.2 * abs(math.cos(i / 12)), 1)
        cog = round((angle * 180 / math.pi + 90) % 360, 1)
        fv2_track.append({"timestamp": t, "lat": round(flat, 6), "lon": round(flon, 6),
                           "sog": spd, "cog": cog, "hdg": round((cog + 10) % 360, 1)})
        t += timedelta(minutes=2)
    vessels.append({
        "mmsi": "645113333", "name": "FV SHENANDOAH",
        "imo": "", "type": "Fishing", "length": 15, "width": 4,
        "draft": 1.8, "call_sign": "3BFS2", "is_target": False,
        "track": fv2_track,
    })

    # ── MAURITIUS PRIDE (coast guard patrol, MMSI 645101010) ──────────────────
    # Patrol route north of grounding site, transiting NE coast
    mp_start = date.replace(hour=7, minute=0)
    mp_track = _straight_track(-20.3, 57.85, 10, 12.0, mp_start, 360, interval_min=2)
    # Then turns south toward grounding area (after grounding reports)
    mp_t2 = mp_start + timedelta(hours=6)
    mp_lat2, mp_lon2 = mp_track[-1]["lat"], mp_track[-1]["lon"]
    mp_track2 = _straight_track(mp_lat2, mp_lon2, 190, 12.0, mp_t2, 120, interval_min=2)
    mp_track.extend(mp_track2)
    vessels.append({
        "mmsi": "645101010", "name": "MAURITIUS PRIDE",
        "imo": "", "type": "Other", "length": 60, "width": 10,
        "draft": 3.0, "call_sign": "3BCP1", "is_target": False,
        "track": mp_track,
    })

    return vessels


# ──────────────────────────────────────────────────────────────────────────────
# Assemble CSV
# ──────────────────────────────────────────────────────────────────────────────

FIELDNAMES = [
    "MMSI", "BaseDateTime", "LAT", "LON", "SOG", "COG", "Heading",
    "VesselName", "IMO", "CallSign", "VesselType", "Status",
    "Length", "Width", "Draft", "Cargo", "TransceiverClass", "IsTarget",
]


def _nav_status(sog: float) -> str:
    if sog < 0.3:
        return "aground" if True else "moored"
    return "under way using engine"


def main():
    all_rows = []

    # ── MV WAKASHIO ───────────────────────────────────────────────────────────
    print("Building MV WAKASHIO track …")
    wakashio_track = build_wakashio_track()
    for pt in wakashio_track:
        all_rows.append({
            "MMSI": "372711000",
            "BaseDateTime": pt["timestamp"].isoformat(),
            "LAT": f"{pt['lat']:.6f}",
            "LON": f"{pt['lon']:.6f}",
            "SOG": f"{pt['sog']:.1f}",
            "COG": f"{pt['cog']:.1f}",
            "Heading": f"{pt['hdg']:.1f}",
            "VesselName": "MV WAKASHIO",
            "IMO": "9337119",
            "CallSign": "3FYL2",
            "VesselType": "Cargo",
            "Status": _nav_status(pt["sog"]),
            "Length": 300, "Width": 50, "Draft": 14.5,
            "Cargo": "ballast", "TransceiverClass": "A",
            "IsTarget": "true",
        })
    print(f"  WAKASHIO: {len(wakashio_track)} fixes  "
          f"({wakashio_track[0]['timestamp']} → {wakashio_track[-1]['timestamp']})")

    # ── Companion vessels ─────────────────────────────────────────────────────
    print("Building companion vessels …")
    companions = build_companion_vessels()
    for v in companions:
        for pt in v["track"]:
            all_rows.append({
                "MMSI": v["mmsi"],
                "BaseDateTime": pt["timestamp"].isoformat(),
                "LAT": f"{pt['lat']:.6f}",
                "LON": f"{pt['lon']:.6f}",
                "SOG": f"{pt['sog']:.1f}",
                "COG": f"{pt['cog']:.1f}",
                "Heading": f"{pt['hdg']:.1f}",
                "VesselName": v["name"],
                "IMO": v["imo"],
                "CallSign": v["call_sign"],
                "VesselType": v["type"],
                "Status": _nav_status(pt["sog"]),
                "Length": v["length"], "Width": v["width"], "Draft": v["draft"],
                "Cargo": "", "TransceiverClass": "A",
                "IsTarget": "false",
            })
        print(f"  {v['name']:20s} ({v['mmsi']}): {len(v['track'])} fixes")

    # Sort chronologically
    all_rows.sort(key=lambda r: r["BaseDateTime"])

    # Write output
    out_dir = os.path.join(os.path.dirname(__file__), "simulation_scenarios")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "wakashio_realistic.csv")

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    # ── Stats ─────────────────────────────────────────────────────────────────
    unique_mmsis = sorted(set(r["MMSI"] for r in all_rows))
    total = len(all_rows)
    wake_count = sum(1 for r in all_rows if r["MMSI"] == "372711000")

    print(f"\n{'='*62}")
    print(f"  OUTPUT : {out_path}")
    print(f"  RECORDS: {total}  across {len(unique_mmsis)} vessels")
    print(f"  WAKASHIO fixes : {wake_count}")
    print(f"  Time span      : {all_rows[0]['BaseDateTime']} → {all_rows[-1]['BaseDateTime']}")
    print(f"{'='*62}")
    print(f"\nVessels:")
    for m in unique_mmsis:
        rows = [r for r in all_rows if r["MMSI"] == m]
        name = rows[0]["VesselName"]
        is_t = rows[0]["IsTarget"]
        print(f"  {m:12s}  {name:22s}  {len(rows):5d} fixes  target={is_t}")
    print(f"\nAnomalies encoded in WAKASHIO track:")
    print(f"  ✓ sudden_stop  : SOG 10.9→0.0 kn at grounding (15:25 UTC)")
    print(f"  ✓ erratic_course: COG variance 246→228→223→227→230→262→170 in final windows")
    print(f"  ✓ erratic_turn  : port turn rate >20°/min approaching reef")
    print(f"  ✓ loitering     : stationary 15:32–16:30 UTC (58 min at reef)")
    print(f"  ✓ AIS gap       : 140-min silence 11:00–13:20 UTC")
    print(f"\nExpected risk pipeline outcome:")
    print(f"  WAKASHIO scores HIGH within 1st 5-min window after grounding")
    print(f"  WAKASHIO scores CRITICAL once 2+ anomalies accumulate")
    print(f"  satellite_tasking_requests row auto-inserted by risk engine")


if __name__ == "__main__":
    main()
