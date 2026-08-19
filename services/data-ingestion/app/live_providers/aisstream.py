"""
AISStream.io WebSocket live AIS provider for the data-ingestion service.

AISStream.io sends real-time AIS messages as JSON over a WebSocket.
Subscription bbox is configured via env vars.
We handle all reconnection logic, keepalive, and message decoding here.

Message types handled:
  - PositionReport (Class A, message types 1/2/3): position + speed + course
  - ExtendedClassBPositionReport (Class B): same fields, different type code
  - ShipStaticData (message type 5): static info (name, type, IMO, destination)
  - StandardClassBPositionReport: class B position

Reference: https://aisstream.io/documentation
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from app.normalizer import normalize_timestamp, normalize_speed, normalize_course, normalize_heading

log = logging.getLogger(__name__)

AISSSTREAM_URL = "wss://stream.aisstream.io/v0/stream"


def _bbox_from_env() -> list:
    """
    Returns the AISStream.io bounding box format:
    [[minLat, minLon], [maxLat, maxLon]]
    """
    return [
        [
            float(os.environ.get("AIS_BBOX_MIN_LAT", 14.0)),
            float(os.environ.get("AIS_BBOX_MIN_LON", 68.0)),
        ],
        [
            float(os.environ.get("AIS_BBOX_MAX_LAT", 25.0)),
            float(os.environ.get("AIS_BBOX_MAX_LON", 77.5)),
        ],
    ]


def _build_subscription(api_key: str) -> str:
    """Build the AISStream.io subscription JSON message."""
    return json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": [_bbox_from_env()],
        "FilterMessageTypes": [
            "PositionReport",
            "ExtendedClassBPositionReport",
            "StandardClassBPositionReport",
            "ShipStaticData",
        ],
    })


def _decode_position_report(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Parse a PositionReport / ExtendedClassBPositionReport message from AISStream.io
    into our internal record format.
    """
    meta = msg.get("MetaData", {})
    msg_type = msg.get("MessageType", "")

    # AISStream wraps the decoded payload in a key named after the message type
    payload = msg.get("Message", {}).get(msg_type, {})
    if not payload:
        # Fallback: some versions of the API use different nesting
        payload = msg.get("Message", {})

    mmsi_raw = meta.get("MMSI") or payload.get("UserID")
    lat_raw = meta.get("latitude") or payload.get("Latitude")
    lon_raw = meta.get("longitude") or payload.get("Longitude")

    if mmsi_raw is None or lat_raw is None or lon_raw is None:
        return None

    # Timestamp: prefer the metadata timestamp from AISStream (server-side receipt time)
    ts_raw = meta.get("time_utc") or meta.get("TimeReceived")
    ts = normalize_timestamp(ts_raw)
    if ts is None:
        ts = datetime.now(timezone.utc)

    speed = normalize_speed(payload.get("Sog") or payload.get("SpeedOverGround"))
    course = normalize_course(payload.get("Cog") or payload.get("CourseOverGround"))
    heading = normalize_heading(payload.get("TrueHeading"))
    nav_status = payload.get("NavigationalStatus")

    return {
        "mmsi": int(mmsi_raw),
        "lat": float(lat_raw),
        "lon": float(lon_raw),
        "speed_knots": speed,
        "course": course,
        "heading": heading,
        "nav_status": nav_status if isinstance(nav_status, int) else None,
        "timestamp": ts,
        "vessel_name": meta.get("ShipName") or meta.get("vessel_name"),
        "raw_source": "aisstream",
        "quality_flag": "raw",
    }


def _decode_ship_static(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Parse a ShipStaticData message from AISStream.io.
    These messages carry vessel metadata (name, IMO, destination, type)
    but no position — we use them to enrich the vessels table.
    """
    meta = msg.get("MetaData", {})
    payload = msg.get("Message", {}).get("ShipStaticData", {})

    mmsi_raw = meta.get("MMSI") or payload.get("UserID")
    if mmsi_raw is None:
        return None

    # Extract ship type code
    ship_type_code = payload.get("Type")

    # Decode dimension fields (AIS sends A/B/C/D bow/stern/port/starboard)
    dim = payload.get("Dimension", {})
    length_m = None
    width_m = None
    if dim:
        a = dim.get("A", 0) or 0  # bow
        b = dim.get("B", 0) or 0  # stern
        c = dim.get("C", 0) or 0  # port
        d = dim.get("D", 0) or 0  # starboard
        if a + b > 0:
            length_m = float(a + b)
        if c + d > 0:
            width_m = float(c + d)

    eta_raw = payload.get("Eta", {})
    eta: Optional[datetime] = None
    if eta_raw and isinstance(eta_raw, dict):
        # AIS ETA is month/day/hour/minute only (no year)
        try:
            now = datetime.now(timezone.utc)
            eta = datetime(
                now.year,
                int(eta_raw.get("Month", 0) or 0) or now.month,
                int(eta_raw.get("Day", 0) or 0) or now.day,
                int(eta_raw.get("Hour", 0) or 0),
                int(eta_raw.get("Minute", 0) or 0),
                tzinfo=timezone.utc,
            )
        except (ValueError, TypeError):
            eta = None

    from shared.geo_utils import decode_ship_type
    vessel_type_str = decode_ship_type(ship_type_code)

    return {
        "mmsi": int(mmsi_raw),
        "vessel_name": (payload.get("Name") or "").strip() or None,
        "vessel_type": ship_type_code,
        "vessel_type_str": vessel_type_str,
        "imo_number": payload.get("ImoNumber"),
        "call_sign": (payload.get("CallSign") or "").strip() or None,
        "draught": payload.get("MaximumStaticDraught"),
        "destination": (payload.get("Destination") or "").strip() or None,
        "eta": eta,
        "length_m": length_m,
        "width_m": width_m,
        "raw_source": "aisstream_static",
        # No position for static messages
        "lat": None,
        "lon": None,
        "timestamp": datetime.now(timezone.utc),
    }


async def stream_ais_records(api_key: str) -> AsyncIterator[Dict[str, Any]]:
    """
    Async generator that yields parsed AIS record dicts from AISStream.io.

    Handles:
    - Reconnection with exponential backoff (5s → 10s → … → 120s cap)
    - Silent periods: AISStream keeps the connection open when no ships are
      transmitting in the bbox — we must NOT reconnect in this case.
      A recv() timeout does NOT mean the connection is dead.
    - Graceful shutdown when the generator is closed

    Uses an asyncio.Queue to decouple the WebSocket recv loop from the generator.
    This avoids incompatibilities between the websockets library's internal keepalive
    and asyncio task cancellation that occur when running inside uvicorn's event loop.

    The producer task runs indefinitely, reconnecting with exponential backoff.
    The generator just reads from the queue.
    """
    subscription = _build_subscription(api_key)
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    _SENTINEL = object()

    async def _producer():
        backoff = 5
        while True:
            try:
                log.info("Connecting to AISStream.io ...")
                async with websockets.connect(
                    AISSSTREAM_URL,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=10,
                    open_timeout=15,
                    max_size=2**20,
                ) as ws:
                    await ws.send(subscription)
                    log.info(
                        "AISStream.io connected. Bbox: lat[%.1f-%.1f] lon[%.1f-%.1f]. "
                        "Waiting for vessel traffic...",
                        float(os.environ.get("AIS_BBOX_MIN_LAT", 14.0)),
                        float(os.environ.get("AIS_BBOX_MAX_LAT", 25.0)),
                        float(os.environ.get("AIS_BBOX_MIN_LON", 68.0)),
                        float(os.environ.get("AIS_BBOX_MAX_LON", 77.5)),
                    )
                    backoff = 5  # reset on successful connect

                    while True:
                        try:
                            raw_msg = await asyncio.wait_for(ws.recv(), timeout=90)
                        except asyncio.TimeoutError:
                            # Server still open — bbox is just quiet right now
                            log.debug("AISStream.io: no messages for 90s — bbox quiet, still connected.")
                            continue

                        try:
                            msg = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            continue

                        msg_type = msg.get("MessageType", "")
                        record: Optional[Dict[str, Any]] = None

                        if msg_type in (
                            "PositionReport",
                            "ExtendedClassBPositionReport",
                            "StandardClassBPositionReport",
                        ):
                            record = _decode_position_report(msg)
                        elif msg_type == "ShipStaticData":
                            record = _decode_ship_static(msg)

                        if record is not None:
                            try:
                                queue.put_nowait(record)
                            except asyncio.QueueFull:
                                log.warning("AISStream queue full — dropping message")

            except asyncio.CancelledError:
                await queue.put(_SENTINEL)
                return

            except (ConnectionClosedOK,):
                log.info("AISStream.io connection closed cleanly.")
                await asyncio.sleep(1)

            except Exception as exc:
                log.warning(
                    "AISStream.io connection lost: %s. Reconnecting in %ds ...",
                    exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    producer_task = asyncio.create_task(_producer())

    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            yield item
    finally:
        producer_task.cancel()
        try:
            await producer_task
        except asyncio.CancelledError:
            pass
