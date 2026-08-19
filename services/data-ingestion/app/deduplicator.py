"""Redis-backed AIS message deduplicator for the data-ingestion service."""
from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

# Deduplicate within this many seconds of the same MMSI
DEDUP_WINDOW_SECONDS = 2
# TTL for dedup sorted-set entries (30 minutes in seconds)
DEDUP_TTL_SECONDS = 1800


class AISDeduplicator:
    """
    Deduplicates AIS messages using a Redis sorted set.

    Key pattern: ais:dedup:{mmsi}
    Members: timestamp epoch as string (rounded to DEDUP_WINDOW_SECONDS)
    Score: timestamp epoch

    A message is a duplicate if we've already seen any message from the same
    MMSI within DEDUP_WINDOW_SECONDS of its timestamp.

    Also detects MMSI-reuse: if the vessel_name changes for the same MMSI
    between consecutive messages, we flag it rather than silently deduplicate.
    """

    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._r = redis_client
        self._name_cache: dict[int, str] = {}  # mmsi -> last known vessel_name

    async def is_duplicate(
        self,
        mmsi: int,
        ts_epoch: float,
        vessel_name: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Returns (is_duplicate, flag_if_any).

        flag_if_any may be:
        - None  : clean message, process normally
        - 'mmsi_reuse' : MMSI seen with a different vessel_name (flag but process)
        """
        key = f"ais:dedup:{mmsi}"
        # Round the epoch to the dedup window
        bucket = int(ts_epoch // DEDUP_WINDOW_SECONDS) * DEDUP_WINDOW_SECONDS
        bucket_str = str(bucket)

        # Check for MMSI reuse (name change)
        flag: Optional[str] = None
        if vessel_name:
            prev_name = self._name_cache.get(mmsi)
            if prev_name is not None and prev_name != vessel_name:
                flag = "mmsi_reuse"
                log.warning(
                    "MMSI %d changed vessel name: %r -> %r (possible MMSI reuse)",
                    mmsi, prev_name, vessel_name
                )
            self._name_cache[mmsi] = vessel_name

        # Check for duplicate timestamp bucket
        score_exists = await self._r.zscore(key, bucket_str)
        if score_exists is not None:
            return True, flag  # duplicate within dedup window

        # Mark this bucket as seen
        pipe = self._r.pipeline(transaction=False)
        pipe.zadd(key, {bucket_str: float(bucket)})
        # Remove entries older than TTL
        expire_before = ts_epoch - DEDUP_TTL_SECONDS
        pipe.zremrangebyscore(key, "-inf", expire_before)
        # Set TTL on the key itself
        pipe.expire(key, DEDUP_TTL_SECONDS)
        await pipe.execute()

        return False, flag
