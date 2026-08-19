#!/usr/bin/env python3
"""
Aqua-Sentinel AIS Reader — Native Host Runner
=============================================
Runs directly on your Mac (not in Docker) so AISStream.io sees your real IP.
Connects to Postgres (port 5433) and Redis (port 6380) on localhost.

Usage:
    pip install asyncpg redis websockets
    python run_ais_reader.py

Or use the Makefile:
    make reader
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ── Environment — defaults match docker-compose port mappings ──────────────────
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5433")
os.environ.setdefault("POSTGRES_USER", "postgres")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres")
os.environ.setdefault("POSTGRES_DB", "maritime_oilspill")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6380")
os.environ.setdefault("AISSTREAM_API_KEY", "8b2b46c7433daad5830de215061d33ca1a792f1a")
os.environ.setdefault("AIS_BBOX_MIN_LAT", "14.0")
os.environ.setdefault("AIS_BBOX_MIN_LON", "68.0")
os.environ.setdefault("AIS_BBOX_MAX_LAT", "25.0")
os.environ.setdefault("AIS_BBOX_MAX_LON", "77.5")

# Add services/shared to path for shared.db / shared.redis_client imports
_repo_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_repo_root, "services"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ais-reader")

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"


def _build_subscription() -> str:
    return json.dumps({
        "APIKey": os.environ["AISSTREAM_API_KEY"],
        "BoundingBoxes": [[
            [float(os.environ["AIS_BBOX_MIN_LAT"]), float(os.environ["AIS_BBOX_MIN_LON"])],
            [float(os.environ["AIS_BBOX_MAX_LAT"]), float(os.environ["AIS_BBOX_MAX_LON"])],
        ]],
        "FilterMessageTypes": [
            "PositionReport",
            "ExtendedClassBPositionReport",
            "StandardClassBCSPositionReport",
            "ShipStaticData",
        ],
    })


def _map_nav_status(code: Optional[int]) -> Optional[str]:
    labels = {
        0: "under way using engine", 1: "at anchor", 2: "not under command",
        3: "restricted manoeuvrability", 4: "constrained by draught", 5: "moored",
        6: "aground", 7: "engaged in fishing", 8: "under way sailing", 15: "undefined",
    }
    return labels.get(code, f"status_{code}") if code is not None else None


def _decode_position(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    meta = msg.get("MetaData", {})
    msg_type = msg.get("MessageType", "")
    payload = msg.get("Message", {}).get(msg_type, {}) or {}

    mmsi_raw = meta.get("MMSI") or payload.get("UserID")
    lat_raw = meta.get("latitude") or payload.get("Latitude")
    lon_raw = meta.get("longitude") or payload.get("Longitude")
    if mmsi_raw is None or lat_raw is None or lon_raw is None:
        return None

    ts_raw = meta.get("time_utc") or meta.get("TimeReceived")
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")) if ts_raw else datetime.now(timezone.utc)
    except Exception:
        ts = datetime.now(timezone.utc)

    sog = payload.get("Sog") or payload.get("SpeedOverGround")
    cog = payload.get("Cog") or payload.get("CourseOverGround")
    hdg = payload.get("TrueHeading")
    nav = payload.get("NavigationalStatus")

    return {
        "mmsi": str(int(mmsi_raw)),
        "lat": float(lat_raw), "lon": float(lon_raw),
        "speed_knots": float(sog) if sog is not None else None,
        "course": float(cog) if cog is not None else None,
        "heading": float(hdg) if hdg is not None else None,
        "nav_status": int(nav) if isinstance(nav, int) else None,
        "timestamp": ts,
        "vessel_name": (meta.get("ShipName") or "").strip() or None,
        "raw_source": "aisstream",
        "quality_flag": "raw",
    }


def _decode_static(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    meta = msg.get("MetaData", {})
    payload = msg.get("Message", {}).get("ShipStaticData", {}) or {}
    mmsi_raw = meta.get("MMSI") or payload.get("UserID")
    if mmsi_raw is None:
        return None
    dim = payload.get("Dimension", {}) or {}
    a, b = dim.get("A", 0) or 0, dim.get("B", 0) or 0
    c, d = dim.get("C", 0) or 0, dim.get("D", 0) or 0
    return {
        "mmsi": str(int(mmsi_raw)),
        "vessel_name": (payload.get("Name") or "").strip() or None,
        "vessel_type_str": None,
        "imo_number": payload.get("ImoNumber"),
        "call_sign": (payload.get("CallSign") or "").strip() or None,
        "draught": payload.get("MaximumStaticDraught"),
        "destination": (payload.get("Destination") or "").strip() or None,
        "length_m": float(a + b) if a + b > 0 else None,
        "width_m": float(c + d) if c + d > 0 else None,
        "lat": None, "lon": None,
        "timestamp": datetime.now(timezone.utc),
        "raw_source": "aisstream_static",
    }


_seen: Dict[str, float] = {}


def _is_duplicate(mmsi: str, ts: float) -> bool:
    last = _seen.get(mmsi)
    if last is not None and abs(ts - last) < 10:
        return True
    _seen[mmsi] = ts
    if len(_seen) > 10000:
        cutoff = time.time() - 300
        for k in [k for k, v in _seen.items() if v < cutoff]:
            del _seen[k]
    return False


async def _process(record: Dict[str, Any], pool, redis) -> None:
    from shared.db import upsert_vessel, insert_position, get_or_create_vessel_id
    from shared.redis_client import publish_to_stream
    try:
        if record.get("lat") is None:
            await upsert_vessel(pool, record)
            return

        lat, lon = record.get("lat"), record.get("lon")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return
        if _is_duplicate(record["mmsi"], record["timestamp"].timestamp()):
            return

        vessel_id = await upsert_vessel(pool, record)
        if vessel_id is None:
            vessel_id = await get_or_create_vessel_id(pool, record["mmsi"])
        if vessel_id is None:
            return

        await insert_position(pool, vessel_id, record)

        nav = record.get("nav_status")
        await publish_to_stream(redis, "ais.clean", {
            "mmsi": record["mmsi"],
            "vessel_id": str(vessel_id),
            "lat": record["lat"],
            "lon": record["lon"],
            "speed_knots": record.get("speed_knots") or "",
            "course": record.get("course") or "",
            "heading": record.get("heading") or "",
            "nav_status": _map_nav_status(nav) if isinstance(nav, int) else (nav or ""),
            "timestamp": record["timestamp"].isoformat(),
            "vessel_name": record.get("vessel_name") or "",
            "vessel_type_str": record.get("vessel_type_str") or "unknown",
            "quality_flag": "raw",
            "raw_source": "aisstream",
        })
    except Exception as e:
        log.error("Process error MMSI %s: %s", record.get("mmsi"), e)


async def main() -> None:
    from shared.db import get_pool
    from shared.redis_client import get_redis

    log.info("=" * 60)
    log.info("Aqua-Sentinel AIS Reader (native host)")
    log.info("Postgres: %s:%s/%s", os.environ["POSTGRES_HOST"], os.environ["POSTGRES_PORT"], os.environ["POSTGRES_DB"])
    log.info("Redis:    %s:%s", os.environ["REDIS_HOST"], os.environ["REDIS_PORT"])
    log.info("Bbox:     lat[%s-%s] lon[%s-%s]",
             os.environ["AIS_BBOX_MIN_LAT"], os.environ["AIS_BBOX_MAX_LAT"],
             os.environ["AIS_BBOX_MIN_LON"], os.environ["AIS_BBOX_MAX_LON"])
    log.info("=" * 60)

    pool = await get_pool()
    redis = await get_redis()
    log.info("DB + Redis connected. Starting AISStream.io connection...")

    import websockets

    subscription = _build_subscription()
    backoff = 60   # Start high — AISStream rate-limits keys that reconnect rapidly
    msg_count = 0

    while True:
        try:
            log.info("Connecting to AISStream.io ...")
            async with websockets.connect(
                AISSTREAM_URL,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=10,
                open_timeout=15,
                max_size=2 ** 20,
            ) as ws:
                await ws.send(subscription)
                log.info("✅ AISStream.io connected and listening. Waiting for vessels...")
                backoff = 5

                while True:
                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=120)
                    except asyncio.TimeoutError:
                        # Bbox is quiet — server still open, just no vessels transmitting
                        log.debug("AISStream quiet for 2min — still connected, waiting...")
                        continue

                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("MessageType", "")
                    record = None
                    if msg_type in ("PositionReport", "ExtendedClassBPositionReport", "StandardClassBCSPositionReport"):
                        record = _decode_position(msg)
                    elif msg_type == "ShipStaticData":
                        record = _decode_static(msg)

                    if record:
                        msg_count += 1
                        if msg_count % 10 == 0:
                            log.info("📡 %d AIS messages processed | last MMSI: %s lat=%.4f lon=%.4f",
                                     msg_count, record.get("mmsi"), record.get("lat", 0), record.get("lon", 0))
                        await _process(record, pool, redis)

        except asyncio.CancelledError:
            log.info("Shutting down.")
            return
        except Exception as exc:
            log.warning("Connection lost: %s — reconnecting in %ds.", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)   # cap at 5 minutes


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user.")
