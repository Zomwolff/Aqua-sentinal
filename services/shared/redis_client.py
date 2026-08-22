"""Async Redis client and stream helpers for Aqua-Sentinel services."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

_client: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """
    Returns the shared Redis client, creating it on first call.
    Retries connection up to 10 times with exponential backoff.
    """
    global _client
    if _client is not None:
        return _client

    host = os.environ.get("REDIS_HOST", "redis")
    port = int(os.environ.get("REDIS_PORT", 6379))

    for attempt in range(1, 11):
        try:
            client = aioredis.Redis(
                host=host,
                port=port,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_keepalive=True,
                health_check_interval=30,
            )
            await client.ping()
            _client = client
            log.info("Redis connected at %s:%s (attempt %d)", host, port, attempt)
            return _client
        except Exception as exc:
            wait = min(2 ** attempt, 30)
            log.warning("Redis connection failed (attempt %d/10): %s — retrying in %ds", attempt, exc, wait)
            if attempt == 10:
                raise
            await asyncio.sleep(wait)

    raise RuntimeError("Unreachable")  # pragma: no cover


async def close_redis() -> None:
    """Gracefully close the Redis connection on shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ──────────────────────────────────────────────────────────────────────────────
# Stream helpers
# ──────────────────────────────────────────────────────────────────────────────

async def publish_to_stream(
    client: aioredis.Redis,
    stream: str,
    data: Dict[str, Any],
    max_len: int = 50_000,
) -> str:
    """
    Publish a dict to a Redis Stream, serialising nested objects to JSON.
    Returns the generated message ID.
    Raises on failure — caller should catch and handle.
    """
    # Redis stream fields must be str -> str; serialise any non-string values
    flat: Dict[str, str] = {}
    for k, v in data.items():
        if isinstance(v, str):
            flat[k] = v
        elif v is None:
            flat[k] = ""
        else:
            flat[k] = json.dumps(v, default=str)

    msg_id = await client.xadd(stream, flat, maxlen=max_len, approximate=True)
    return msg_id


async def ensure_consumer_group(
    client: aioredis.Redis,
    stream: str,
    group: str,
) -> None:
    """
    Create a consumer group on the stream, starting from the beginning of time.
    """

    try:
        await client.xgroup_create(stream, group, id="0", mkstream=True)
        log.info("Consumer group '%s' created on stream '%s'", group, stream)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            log.debug("Consumer group '%s' already exists on '%s'", group, stream)
        else:
            raise


async def consume_stream(
    client: aioredis.Redis,
    stream: str,
    group: str,
    consumer: str,
    count: int = 100,
    block_ms: int = 2000,
) -> List[Dict[str, Any]]:
    """
    Read pending + new messages from a Redis Stream consumer group.
    Returns a list of {id, data} dicts.
    Automatically acknowledges each message after returning it.

    The caller is responsible for processing before the next call so that
    unprocessed messages are re-delivered on the next block.
    """
    try:
        results = await client.xreadgroup(
            groupname=group,
            consumername=consumer,
            streams={stream: ">"},
            count=count,
            block=block_ms,
        )
    except Exception as exc:
        log.error("xreadgroup failed on stream %s: %s", stream, exc)
        return []

    messages = []
    if not results:
        return messages

    for _stream_name, entries in results:
        for msg_id, data in entries:
            messages.append({"id": msg_id, "data": data})
            # Acknowledge immediately — we process synchronously
            try:
                await client.xack(stream, group, msg_id)
            except Exception as ack_exc:
                log.warning("xack failed for %s on %s: %s", msg_id, stream, ack_exc)

    return messages


# ──────────────────────────────────────────────────────────────────────────────
# Simple key/value helpers (for trust scores, STS state, etc.)
# ──────────────────────────────────────────────────────────────────────────────

async def hset_json(client: aioredis.Redis, key: str, mapping: Dict[str, Any]) -> None:
    """Store a dict in a Redis hash, JSON-encoding non-string values."""
    flat = {k: json.dumps(v, default=str) if not isinstance(v, str) else v
            for k, v in mapping.items()}
    await client.hset(key, mapping=flat)


async def hget_json(client: aioredis.Redis, key: str) -> Dict[str, Any]:
    """Retrieve a Redis hash and JSON-decode all values."""
    raw = await client.hgetall(key)
    result: Dict[str, Any] = {}
    for k, v in raw.items():
        try:
            result[k] = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            result[k] = v
    return result


async def zadd_capped(
    client: aioredis.Redis,
    key: str,
    score: float,
    member: str,
    max_size: int = 10_000,
) -> None:
    """
    Add a member to a sorted set and trim to max_size oldest entries.
    Used for rolling windows keyed by timestamp.
    """
    pipe = client.pipeline(transaction=False)
    pipe.zadd(key, {member: score})
    pipe.zremrangebyrank(key, 0, -(max_size + 1))
    await pipe.execute()
