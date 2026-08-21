"""
Standalone AIS Stream Reader — pure Python, no uvicorn, no HTTP server.

This service:
1. Opens a persistent WebSocket to AISStream.io
2. Validates, deduplicates, and normalises every AIS message
3. Writes to Postgres (vessels + vessel_positions tables)
4. Publishes to Redis Stream 'ais.clean' for downstream services

Run as: python -m app.reader
This is intentionally separate from the HTTP data-ingestion service so the
WebSocket connection runs in a clean asyncio event loop without any HTTP
server interference.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

sys.path.insert(0, "/app")

from shared.db import get_pool, upsert_vessel, insert_position, insert_draught_observation, get_or_create_vessel_id
from shared.redis_client import get_redis, publish_to_stream

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ais-reader")

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
VESSELAPI_URL = "https://api.vesselapi.com/v1/location/vessels/bounding-box"


def _bbox_from_env() -> list:
    return [
        [float(os.environ.get("AIS_BBOX_MIN_LAT", 14.0)), float(os.environ.get("AIS_BBOX_MIN_LON", 68.0))],
        [float(os.environ.get("AIS_BBOX_MAX_LAT", 25.0)), float(os.environ.get("AIS_BBOX_MAX_LON", 77.5))],
    ]


def _build_subscription(api_key: str) -> str:
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


def _vesselapi_bbox_from_env() -> Dict[str, float]:
    """Return a compact Mumbai-harbor area within VesselAPI's 4° span limit."""
    return {
        "lat_bottom": float(os.environ.get("VESSELAPI_BBOX_MIN_LAT", "18.4")),
        "lat_top": float(os.environ.get("VESSELAPI_BBOX_MAX_LAT", "19.8")),
        "lon_left": float(os.environ.get("VESSELAPI_BBOX_MIN_LON", "71.5")),
        "lon_right": float(os.environ.get("VESSELAPI_BBOX_MAX_LON", "73.0")),
    }


def _fetch_vesselapi_page(api_key: str) -> list[Dict[str, Any]]:
    """Fetch current positions without ever logging the API key or request URL."""
    bbox = _vesselapi_bbox_from_env()
    params = {
        "filter.latBottom": bbox["lat_bottom"],
        "filter.latTop": bbox["lat_top"],
        "filter.lonLeft": bbox["lon_left"],
        "filter.lonRight": bbox["lon_right"],
        "pagination.limit": 50,
    }
    request = Request(
        f"{VESSELAPI_URL}?{urlencode(params)}",
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "Aqua-Sentinel/1.0"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"VesselAPI request failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"VesselAPI network error: {exc.reason}") from exc

    if payload.get("error"):
        raise RuntimeError(f"VesselAPI error: {payload['error'].get('code', 'unknown')}")
    return payload.get("vessels", [])


def _decode_vesselapi_position(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a VesselAPI position response onto the shared AIS record model."""
    try:
        mmsi = str(int(item["mmsi"]))
        lat, lon = float(item["latitude"]), float(item["longitude"])
    except (KeyError, TypeError, ValueError):
        return None

    timestamp = item.get("timestamp") or item.get("processed_timestamp")
    try:
        timestamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        timestamp = datetime.now(timezone.utc)

    return {
        "mmsi": mmsi,
        "lat": lat,
        "lon": lon,
        "speed_knots": item.get("sog"),
        "course": item.get("cog"),
        "heading": item.get("heading"),
        "nav_status": item.get("nav_status"),
        "timestamp": timestamp,
        "vessel_name": item.get("vessel_name"),
        "imo_number": item.get("imo"),
        "raw_source": "vesselapi",
        "quality_flag": "suspected_glitch" if item.get("suspected_glitch") else "raw",
    }


def _provider_error(msg: Dict[str, Any]) -> Optional[str]:
    """Return a provider-side subscription error without logging raw payloads."""
    for key in ("error", "Error", "error_message", "ErrorMessage", "message"):
        value = msg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    message_type = str(msg.get("MessageType", "")).lower()
    if "error" in message_type or "reject" in message_type:
        return f"AISStream returned {msg.get('MessageType')}"
    return None


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
    payload = msg.get("Message", {}).get(msg_type, {}) or msg.get("Message", {})

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
    payload = msg.get("Message", {}).get("ShipStaticData", {})
    mmsi_raw = meta.get("MMSI") or payload.get("UserID")
    if mmsi_raw is None:
        return None

    dim = payload.get("Dimension", {}) or {}
    a, b = dim.get("A", 0) or 0, dim.get("B", 0) or 0
    c, d = dim.get("C", 0) or 0, dim.get("D", 0) or 0

    return {
        "mmsi": str(int(mmsi_raw)),
        "vessel_name": (payload.get("Name") or "").strip() or None,
        "vessel_type": payload.get("Type"),
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
_DEDUP_WINDOW_S = 10


def _is_duplicate(mmsi: str, ts: float) -> bool:
    key = mmsi
    last = _seen.get(key)
    if last is not None and abs(ts - last) < _DEDUP_WINDOW_S:
        return True
    _seen[key] = ts
    # Prune old entries every 1000 messages
    if len(_seen) > 10000:
        cutoff = time.time() - 300
        to_del = [k for k, v in _seen.items() if v < cutoff]
        for k in to_del:
            del _seen[k]
    return False


async def _process(record: Dict[str, Any], pool, redis) -> None:
    try:
        is_static = record.get("lat") is None
        if is_static:
            await upsert_vessel(pool, record)
            return

        lat, lon = record.get("lat"), record.get("lon")
        if lat is None or lon is None:
            return
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return

        mmsi = record["mmsi"]
        ts = record["timestamp"].timestamp()
        if _is_duplicate(mmsi, ts):
            return

        vessel_id = await upsert_vessel(pool, record)
        if vessel_id is None:
            vessel_id = await get_or_create_vessel_id(pool, mmsi)
        if vessel_id is None:
            return

        await insert_position(pool, vessel_id, record)
        await insert_draught_observation(pool, vessel_id, record)

        nav_raw = record.get("nav_status")
        await publish_to_stream(redis, "ais.clean", {
            "mmsi": mmsi,
            "vessel_id": str(vessel_id),
            "lat": record["lat"],
            "lon": record["lon"],
            "speed_knots": record.get("speed_knots") or "",
            "course": record.get("course") or "",
            "heading": record.get("heading") or "",
            "nav_status": _map_nav_status(nav_raw) if isinstance(nav_raw, int) else (nav_raw or ""),
            "timestamp": record["timestamp"].isoformat(),
            "vessel_name": record.get("vessel_name") or "",
            "vessel_type_str": record.get("vessel_type_str") or "unknown",
            "quality_flag": record.get("quality_flag", "raw"),
            "raw_source": record.get("raw_source", "aisstream"),
            "draught": record.get("draught") or "",
        })

    except Exception as e:
        log.error("Process error for MMSI %s: %s", record.get("mmsi"), e)


CONTROL_CHANNEL = "control.fetch_ais"
FETCH_STATUS_STREAM = "ais.fetch.status"


async def _publish_fetch_status(redis, *, trigger: str, received: int, processed: int,
                                detail: str = "") -> None:
    """Publish a fetch-cycle summary so the gateway/UI can show live progress."""
    try:
        await publish_to_stream(redis, FETCH_STATUS_STREAM, {
            "at": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "received": str(received),
            "processed": str(processed),
            "detail": detail,
        })
    except Exception as exc:
        log.warning("Failed to publish %s: %s", FETCH_STATUS_STREAM, exc)


async def _run_vesselapi(api_key: str, pool, redis) -> None:
    interval = max(15, int(os.environ.get("VESSELAPI_POLL_INTERVAL_SECONDS", "60")))
    bbox = _vesselapi_bbox_from_env()
    log.info(
        "VesselAPI polling enabled every %ss. Bbox: lat[%.2f-%.2f] lon[%.2f-%.2f].",
        interval, bbox["lat_bottom"], bbox["lat_top"], bbox["lon_left"], bbox["lon_right"],
    )

    # Manual-fetch support: a message on control.fetch_ais interrupts the sleep
    # and triggers an immediate poll cycle (UI "Fetch Live AIS" button).
    trigger_event: asyncio.Event = asyncio.Event()
    pubsub = None
    try:
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(CONTROL_CHANNEL)
    except Exception as exc:
        log.warning("Control channel subscribe failed (%s) — manual fetch disabled.", exc)

    async def _control_listener() -> None:
        if pubsub is None:
            return
        try:
            async for msg in pubsub.listen():
                if msg and msg.get("type") == "message":
                    trigger_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Control channel listener error: %s", exc)

    listener_task = asyncio.create_task(_control_listener()) if pubsub else None

    triggered = False
    while True:
        try:
            items = await asyncio.to_thread(_fetch_vesselapi_page, api_key)
            processed = 0
            for item in items:
                record = _decode_vesselapi_position(item)
                if record:
                    processed += 1
                    await _process(record, pool, redis)
            log.info(
                "VesselAPI poll received %d positions; processed %d (%s).",
                len(items), processed, "manual" if triggered else "scheduled",
            )
            await _publish_fetch_status(
                redis,
                trigger="manual" if triggered else "scheduled",
                received=len(items),
                processed=processed,
            )
        except asyncio.CancelledError:
            if listener_task:
                listener_task.cancel()
            raise
        except Exception as exc:
            log.warning("VesselAPI poll failed: %s", exc)
            await _publish_fetch_status(
                redis, trigger="manual" if triggered else "scheduled",
                received=0, processed=0, detail=str(exc),
            )
        triggered = False

        # Interruptible sleep: wake early when a manual fetch is requested.
        try:
            await asyncio.wait_for(trigger_event.wait(), timeout=interval)
            trigger_event.clear()
            triggered = True
            log.info("Manual AIS fetch requested — polling VesselAPI now.")
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    provider = os.environ.get("AIS_PROVIDER", "aisstream").lower()
    key_name = "VESSELAPI_API_KEY" if provider == "vesselapi" else "AISSTREAM_API_KEY"
    api_key = os.environ.get(key_name, "")
    if not api_key:
        log.error("%s not set — exiting.", key_name)
        return

    log.info("AIS Reader starting up...")
    pool = await get_pool()
    redis = await get_redis()
    log.info("DB + Redis connected. Connecting to AISStream.io...")

    if provider == "vesselapi":
        await _run_vesselapi(api_key, pool, redis)
        return
    if provider != "aisstream":
        log.error("Unsupported AIS_PROVIDER=%s — exiting.", provider)
        return

    import websockets  # noqa

    # In continuous-stream mode a manual fetch request cannot "poll again" —
    # acknowledge it on the status stream so the UI feed reflects the action.
    async def _control_ack() -> None:
        try:
            pubsub = redis.pubsub(ignore_subscribe_messages=True)
            await pubsub.subscribe(CONTROL_CHANNEL)
            async for msg in pubsub.listen():
                if msg and msg.get("type") == "message":
                    await _publish_fetch_status(
                        redis, trigger="manual", received=-1, processed=0,
                        detail="continuous stream active — live telemetry already flowing",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Control ack listener error: %s", exc)

    ack_task = asyncio.create_task(_control_ack())

    subscription = _build_subscription(api_key)
    backoff = 5
    msg_count = 0

    while True:
        try:
            async with websockets.connect(
                AISSTREAM_URL,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=10,
                open_timeout=15,
                max_size=2**20,
            ) as ws:
                await ws.send(subscription)
                log.info(
                    "AISStream.io connected. Bbox: lat[%s-%s] lon[%s-%s]. Listening for vessels...",
                    os.environ.get("AIS_BBOX_MIN_LAT", "14.0"),
                    os.environ.get("AIS_BBOX_MAX_LAT", "25.0"),
                    os.environ.get("AIS_BBOX_MIN_LON", "68.0"),
                    os.environ.get("AIS_BBOX_MAX_LON", "77.5"),
                )
                # Do not reset retry delay merely because the TCP/WebSocket
                # handshake succeeded.  If AISStream closes immediately after
                # subscription, reconnecting every five seconds creates a retry
                # storm and can trigger provider-side throttling.  A real data
                # message proves that the subscription is healthy.
                received_message = False

                while True:
                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=120)
                    except asyncio.TimeoutError:
                        log.debug("AISStream quiet for 2min — still connected.")
                        continue

                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        log.warning("AISStream sent a non-JSON frame; reconnecting.")
                        raise RuntimeError("AISStream sent a non-JSON frame")

                    if not isinstance(msg, dict):
                        log.warning("AISStream sent an unexpected JSON payload; reconnecting.")
                        raise RuntimeError("AISStream sent an unexpected JSON payload")

                    error = _provider_error(msg)
                    if error:
                        # AISStream reports rejected subscriptions as regular
                        # WebSocket messages.  Previously these were ignored,
                        # making a bad key/account/rate-limit look like a
                        # mysterious transport disconnect.
                        raise RuntimeError(f"AISStream rejected subscription: {error}")

                    if not received_message:
                        received_message = True
                        backoff = 5
                        log.info("AISStream subscription confirmed by first data message.")

                    msg_type = msg.get("MessageType", "")
                    record = None

                    if msg_type in (
                        "PositionReport",
                        "ExtendedClassBPositionReport",
                        "StandardClassBPositionReport",
                    ):
                        record = _decode_position(msg)
                    elif msg_type == "ShipStaticData":
                        record = _decode_static(msg)

                    if record:
                        msg_count += 1
                        if msg_count % 100 == 0:
                            log.info("Processed %d AIS messages so far.", msg_count)
                        await _process(record, pool, redis)

        except asyncio.CancelledError:
            log.info("AIS Reader cancelled — shutting down.")
            return
        except websockets.exceptions.ConnectionClosed as exc:
            log.warning(
                "AISStream connection closed: code=%s reason=%s — reconnecting in %ds.",
                exc.code,
                exc.reason or "<none>",
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
        except Exception as exc:
            log.warning("AISStream connection lost: %s — reconnecting in %ds.", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


if __name__ == "__main__":
    asyncio.run(main())
