"""
Redis-backed rolling window buffer for AIS pings per vessel.

Each MMSI gets a sorted set: ais:window:{mmsi}
  - Member: JSON-serialised ping dict
  - Score: Unix timestamp epoch

Window is capped at WINDOW_MINUTES of data. Old entries are evicted by score
(timestamp) not by count, so gaps in AIS reporting are preserved correctly.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", 15))
MAX_PINGS_PER_VESSEL = 500  # hard cap to prevent runaway Redis memory usage


class WindowBuffer:
    """
    Rolling time-window buffer for AIS pings, backed by Redis sorted sets.

    Key pattern: ais:window:{mmsi}
    Score: Unix timestamp epoch (float)
    Value: JSON-encoded ping dict

    Thread safety: async-safe (single-threaded asyncio event loop).
    """

    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._r = redis_client

    async def add_ping(self, mmsi: int, ping: Dict[str, Any]) -> None:
        """
        Add a ping to the rolling window for this MMSI.
        Evicts pings older than WINDOW_MINUTES automatically.
        """
        key = f"ais:window:{mmsi}"
        ts_raw = ping.get("timestamp")

        # Resolve timestamp to epoch
        if isinstance(ts_raw, datetime):
            ts_epoch = ts_raw.timestamp()
        elif isinstance(ts_raw, (int, float)):
            ts_epoch = float(ts_raw)
        elif isinstance(ts_raw, str):
            try:
                dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                ts_epoch = dt.timestamp()
            except ValueError:
                log.warning("Cannot parse timestamp for MMSI %d: %r", mmsi, ts_raw)
                return
        else:
            log.warning("Unknown timestamp type for MMSI %d: %r", mmsi, type(ts_raw))
            return

        # Serialise the ping with the epoch embedded for easy retrieval
        ping_data = dict(ping)
        ping_data["ts_epoch"] = ts_epoch
        member = json.dumps(ping_data, default=str)

        cutoff_epoch = ts_epoch - (WINDOW_MINUTES * 60)

        pipe = self._r.pipeline(transaction=False)
        pipe.zadd(key, {member: ts_epoch})
        # Evict entries older than the window
        pipe.zremrangebyscore(key, "-inf", cutoff_epoch)
        # Cap total entries
        pipe.zremrangebyrank(key, 0, -(MAX_PINGS_PER_VESSEL + 1))
        # Keep key alive for 2x the window duration
        pipe.expire(key, WINDOW_MINUTES * 60 * 2)
        await pipe.execute()

    async def get_window(
        self,
        mmsi: int,
        window_start: Optional[float] = None,
        window_end: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return all pings for this MMSI within [window_start, window_end] epoch range.
        If window_start/end are None, returns the full current window.
        """
        key = f"ais:window:{mmsi}"
        min_score = window_start if window_start is not None else "-inf"
        max_score = window_end if window_end is not None else "+inf"

        members = await self._r.zrangebyscore(key, min_score, max_score)
        pings = []
        for m in members:
            try:
                pings.append(json.loads(m))
            except json.JSONDecodeError:
                log.warning("Invalid JSON in window buffer for MMSI %d", mmsi)
        return pings

    async def get_active_mmsis(self) -> List[int]:
        """
        Return a list of MMSIs that currently have data in the buffer.
        Used for cross-vessel proximity checks.
        """
        keys = await self._r.keys("ais:window:*")
        mmsis = []
        for key in keys:
            try:
                mmsis.append(int(key.split(":")[-1]))
            except ValueError:
                pass
        return mmsis

    async def get_last_ping(self, mmsi: int) -> Optional[Dict[str, Any]]:
        """
        Return the most recent ping for this MMSI, or None if the window is empty.
        """
        key = f"ais:window:{mmsi}"
        members = await self._r.zrange(key, -1, -1)  # last element
        if not members:
            return None
        try:
            return json.loads(members[0])
        except (json.JSONDecodeError, IndexError):
            return None

    async def clear_window(self, mmsi: int) -> None:
        """Remove all pings for this MMSI from the buffer."""
        await self._r.delete(f"ais:window:{mmsi}")
