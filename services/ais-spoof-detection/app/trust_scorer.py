"""
AIS Trust Score computation — 4 independent factors combined into instant + rolling EMA.

Factors:
  1. Speed jump    — implied velocity between consecutive pings > MAX_PHYSICAL_SPEED_MS
  2. Identity      — vessel name or type changed for same MMSI
  3. MMSI validity — ITU MID validation
  4. Positional    — AIS vs satellite discrepancy (when satellite data available)

Rolling score uses EMA so it converges smoothly rather than jumping on single events.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

MAX_PHYSICAL_SPEED_MS = float(os.environ.get("MAX_PHYSICAL_SPEED_MS", 25.7))   # ~50 kn
MAX_PLAUSIBLE_AIS_ERROR_M = float(os.environ.get("MAX_PLAUSIBLE_AIS_ERROR_M", 500.0))
SPOOFING_TRUST_THRESHOLD = 0.5
EMA_ALPHA = 0.3  # weight for newest observation


def score_speed_jump(
    lat1: float, lon1: float, ts1: datetime,
    lat2: float, lon2: float, ts2: datetime,
) -> Tuple[float, bool]:
    """Trust = 1.0 if speed plausible, 0.0 if teleportation detected."""
    from shared.geo_utils import implied_speed_ms
    speed_ms = implied_speed_ms(lat1, lon1, ts1, lat2, lon2, ts2)
    if speed_ms is None:
        return 1.0, False
    if speed_ms > MAX_PHYSICAL_SPEED_MS:
        log.warning("Speed jump: %.1f m/s (%.1f kn)", speed_ms, speed_ms * 1.944)
        return 0.0, True
    return 1.0, False


def score_identity_consistency(
    mmsi: int,
    current_name: Optional[str], current_type: Optional[str],
    previous_name: Optional[str], previous_type: Optional[str],
) -> Tuple[float, bool]:
    """Returns (trust_component, identity_changed)."""
    if previous_name is None and previous_type is None:
        return 1.0, False
    name_changed = (current_name and previous_name and
                    current_name.strip().lower() != previous_name.strip().lower())
    type_changed = (current_type and previous_type and
                    current_type.strip().lower() != previous_type.strip().lower())
    if name_changed or type_changed:
        log.warning("Identity change MMSI %d: name %r->%r type %r->%r",
                    mmsi, previous_name, current_name, previous_type, current_type)
        return 0.0, True
    return 1.0, False


def score_mmsi_validity(mmsi: int) -> Tuple[float, bool]:
    """Returns (trust_component, is_valid)."""
    from shared.geo_utils import validate_mmsi
    is_valid, reason = validate_mmsi(mmsi)
    if not is_valid:
        log.warning("Invalid MMSI %d: %s", mmsi, reason)
        return 0.8, False
    return 1.0, True


def score_positional_discrepancy(
    ais_lat: float, ais_lon: float,
    sat_lat: float, sat_lon: float,
    max_error_m: float = MAX_PLAUSIBLE_AIS_ERROR_M,
) -> Tuple[float, float]:
    """Returns (trust_component, discrepancy_m). Component linearly decays from 1 to 0."""
    from shared.geo_utils import haversine_distance
    discrepancy_m = haversine_distance(ais_lat, ais_lon, sat_lat, sat_lon)
    trust = max(0.0, 1.0 - discrepancy_m / max_error_m)
    return trust, discrepancy_m


def compute_instant_trust_score(
    speed_jump_trust: float,
    identity_trust: float,
    mmsi_trust: float,
    position_trust: float = 1.0,
) -> float:
    """Weighted combination: position(0.5) + speed_jump(0.25) + identity(0.15) + mmsi(0.10)."""
    return (0.50 * position_trust + 0.25 * speed_jump_trust
            + 0.15 * identity_trust + 0.10 * mmsi_trust)


async def update_rolling_trust(redis, mmsi: int, instant_score: float) -> float:
    """EMA update of rolling trust score in Redis. Returns new rolling score."""
    key = f"ais:trust:{mmsi}"
    raw = await redis.hgetall(key)
    prev_rolling = float(raw.get("rolling_score", 1.0))
    count = int(float(raw.get("observation_count", 0))) + 1
    new_rolling = EMA_ALPHA * instant_score + (1 - EMA_ALPHA) * prev_rolling
    await redis.hset(key, mapping={
        "rolling_score": str(new_rolling),
        "observation_count": str(count),
        "instant_score": str(instant_score),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    await redis.expire(key, 86400)
    return new_rolling


async def get_rolling_trust_score(redis, mmsi: int) -> float:
    """Return current rolling trust score (1.0 = fully trusted, default for unknowns)."""
    val = await redis.hget(f"ais:trust:{mmsi}", "rolling_score")
    try:
        return float(val) if val is not None else 1.0
    except (ValueError, TypeError):
        return 1.0
