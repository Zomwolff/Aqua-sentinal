"""
AIS Trust Score computation — 5 independent factors combined into instant + rolling EMA.

Factors (production-grade):
  1. Dead-reckoning position (35%) — Given the last ping's lat/lon/speed/heading and elapsed
     time, compute the kinematically expected position using a standard dead-reckoning model.
     Compare to actual reported position. A large discrepancy implies teleportation/spoofing.
  2. Speed jump (25%) — Raw implied velocity between consecutive pings > MAX_PHYSICAL_SPEED_MS
     (catches sudden large jumps that dead-reckoning might not if heading data is missing).
  3. MMSI collision (20%) — The same MMSI is reporting from two geographically impossible
     locations simultaneously (i.e., a cloned MMSI or phantom replay attack).
  4. Identity consistency (15%) — Vessel name or type changed for the same MMSI between pings.
  5. MMSI validity (5%) — ITU MID prefix validation (country code plausibility).

Rolling score uses EMA so it converges smoothly rather than jumping on single events.
A vessel starts fully trusted (rolling=1.0) and loses trust as suspicious pings accumulate.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

MAX_PHYSICAL_SPEED_MS     = float(os.environ.get("MAX_PHYSICAL_SPEED_MS", 25.7))    # ~50 kn
MAX_PLAUSIBLE_AIS_ERROR_M = float(os.environ.get("MAX_PLAUSIBLE_AIS_ERROR_M", 500.0))
SPOOFING_TRUST_THRESHOLD  = 0.5
EMA_ALPHA                 = 0.3   # weight for newest observation
MMSI_COLLISION_RADIUS_M   = 50_000.0  # 50 km — beyond this simultaneously = impossible


# ── Factor 1: Dead-reckoning position validation ──────────────────────────────

def _dead_reckon(
    lat: float, lon: float,
    speed_knots: float, heading_deg: float,
    elapsed_s: float,
) -> Tuple[float, float]:
    """
    Project a position forward using speed and heading.
    Uses simple flat-earth approximation (accurate to <0.1% over <200km).
    Returns (predicted_lat, predicted_lon).
    """
    speed_ms = speed_knots * 0.514444
    distance_m = speed_ms * elapsed_s

    # Convert heading to math angle (0°=East, CCW)
    heading_rad = math.radians(90.0 - heading_deg)
    dx = distance_m * math.cos(heading_rad)   # East displacement (m)
    dy = distance_m * math.sin(heading_rad)   # North displacement (m)

    # 1 degree lat ≈ 111_319 m; 1 degree lon ≈ 111_319 * cos(lat) m
    dlat = dy / 111_319.0
    dlon = dx / (111_319.0 * math.cos(math.radians(lat)) + 1e-10)
    return lat + dlat, lon + dlon


def score_dead_reckoning(
    prev_lat: float, prev_lon: float,
    prev_speed_knots: float, prev_heading_deg: float,
    ts1: datetime,
    curr_lat: float, curr_lon: float,
    ts2: datetime,
    max_error_m: float = MAX_PLAUSIBLE_AIS_ERROR_M,
) -> Tuple[float, float]:
    """
    Returns (trust_component, discrepancy_m).
    Trust component linearly decays 1→0 as discrepancy grows from 0→max_error_m.

    A ship travelling at 15 knots for 10 minutes covers ~4.6 km.
    We allow for GPS inaccuracy (AIS ≈ ±100m) + AIS reporting lag.
    max_error_m is scaled by elapsed time and speed to avoid penalising
    fast vessels over long intervals.
    """
    from shared.geo_utils import haversine_distance

    elapsed_s = abs((ts2 - ts1).total_seconds())
    if elapsed_s < 10:
        # Too close together — dead-reckoning unreliable
        return 1.0, 0.0

    pred_lat, pred_lon = _dead_reckon(prev_lat, prev_lon, prev_speed_knots, prev_heading_deg, elapsed_s)
    discrepancy_m = haversine_distance(pred_lat, pred_lon, curr_lat, curr_lon)

    # Dynamic tolerance: allow wider error for fast vessels over longer intervals
    tolerance_m = max(max_error_m, prev_speed_knots * 0.514444 * elapsed_s * 0.15)
    trust = max(0.0, 1.0 - discrepancy_m / tolerance_m)
    return trust, discrepancy_m


# ── Factor 2: Raw speed jump ──────────────────────────────────────────────────

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


# ── Factor 3: MMSI collision (phantom/cloned MMSI) ────────────────────────────

async def score_mmsi_collision(pool, mmsi: int, curr_lat: float, curr_lon: float) -> Tuple[float, bool]:
    """
    Checks if the same MMSI has a recent position report that is geographically
    incompatible with the current report (i.e., two simultaneous positions >50km apart).
    Returns (trust_component, collision_detected).
    """
    try:
        from shared.geo_utils import haversine_distance
        # Get the most recent position in the DB for this MMSI
        row = await pool.fetchrow(
            """SELECT last_lat, last_lon, last_seen FROM vessels WHERE mmsi=$1""",
            str(mmsi),
        )
        if not row or row["last_lat"] is None:
            return 1.0, False

        prev_lat = float(row["last_lat"])
        prev_lon = float(row["last_lon"])
        last_seen = row["last_seen"]

        if last_seen is None:
            return 1.0, False

        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)

        elapsed_s = (datetime.now(timezone.utc) - last_seen).total_seconds()

        # Max possible distance at top physical speed
        max_possible_m = MAX_PHYSICAL_SPEED_MS * elapsed_s
        actual_m = haversine_distance(prev_lat, prev_lon, curr_lat, curr_lon)

        if actual_m > max_possible_m + MMSI_COLLISION_RADIUS_M:
            log.warning(
                "MMSI COLLISION detected for MMSI %d: %.1f km apart in %.0fs (max possible %.1f km)",
                mmsi, actual_m / 1000, elapsed_s, max_possible_m / 1000,
            )
            return 0.0, True

        return 1.0, False

    except Exception as e:
        log.debug("MMSI collision check failed: %s", e)
        return 1.0, False


# ── Factor 4: Identity consistency ───────────────────────────────────────────

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


# ── Factor 5: MMSI validity ───────────────────────────────────────────────────

def score_mmsi_validity(mmsi: int) -> Tuple[float, bool]:
    """Returns (trust_component, is_valid)."""
    from shared.geo_utils import validate_mmsi
    is_valid, reason = validate_mmsi(mmsi)
    if not is_valid:
        log.warning("Invalid MMSI %d: %s", mmsi, reason)
        return 0.8, False
    return 1.0, True


# ── Combined score ─────────────────────────────────────────────────────────────

def compute_instant_trust_score(
    dr_trust: float,          # dead-reckoning (35%)
    speed_jump_trust: float,  # raw speed jump  (25%)
    collision_trust: float,   # MMSI collision  (20%)
    identity_trust: float,    # identity change (15%)
    mmsi_trust: float,        # MMSI validity   ( 5%)
) -> float:
    """5-factor weighted combination → [0, 1]."""
    return (
        0.35 * dr_trust
        + 0.25 * speed_jump_trust
        + 0.20 * collision_trust
        + 0.15 * identity_trust
        + 0.05 * mmsi_trust
    )


# ── Rolling EMA ───────────────────────────────────────────────────────────────

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

