"""
Async PostgreSQL connection pool for Aqua-Sentinel services.

Key schema facts (v2):
  - vessels.id          = BIGSERIAL PK (internal)
  - vessels.mmsi        = VARCHAR(20) UNIQUE NOT NULL (AIS join key)
  - vessel_positions.vessel_id = BIGINT FK → vessels.id
  - vessel_positions uses latitude/longitude/course_deg/heading_deg column names
  - geom is auto-populated by DB trigger (no need to compute in Python)
  - All pipeline tables (features, anomalies, trust, sts, risk) use vessel_id FK + mmsi denorm
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

log = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    """
    Returns the shared asyncpg connection pool, creating it if necessary.
    Retries up to 10 times with exponential backoff to handle container startup
    races where PostgreSQL may not be ready yet.
    """
    global _pool
    if _pool is not None:
        return _pool

    dsn = (
        f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ['POSTGRES_HOST']}:{os.environ.get('POSTGRES_PORT', 5432)}"
        f"/{os.environ['POSTGRES_DB']}"
    )

    for attempt in range(1, 11):
        try:
            _pool = await asyncpg.create_pool(
                dsn,
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            log.info("PostgreSQL pool created (attempt %d)", attempt)
            return _pool
        except Exception as exc:
            wait = min(2 ** attempt, 30)
            log.warning("PG connection failed (attempt %d/10): %s — retrying in %ds", attempt, exc, wait)
            if attempt == 10:
                raise
            await asyncio.sleep(wait)

    raise RuntimeError("Unreachable")  # pragma: no cover


async def close_pool() -> None:
    """Gracefully close the pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ──────────────────────────────────────────────────────────────────────────────
# Vessel helpers (schema v2: varchar mmsi, bigserial id)
# ──────────────────────────────────────────────────────────────────────────────

def _map_vessel_type(vessel_type_str: Optional[str]) -> str:
    """
    Map free-text vessel type to the vessel_type_enum values in the DB.
    AISStream sends human-readable strings; we normalise them.
    """
    if not vessel_type_str:
        return "unknown"
    vt = vessel_type_str.lower().strip()
    mapping = {
        "tanker": "tanker", "oil tanker": "tanker", "chemical tanker": "tanker",
        "cargo": "cargo", "container": "cargo", "container ship": "cargo",
        "fishing": "fishing",
        "tug": "tug", "tugboat": "tug",
        "passenger": "passenger", "ferry": "passenger",
        "high speed": "high_speed", "high_speed": "high_speed", "hsc": "high_speed",
        "sailing": "sailing", "sailboat": "sailing",
        "pleasure": "pleasure", "yacht": "pleasure",
        "pilot": "pilot", "pilot vessel": "pilot",
        "sar": "search_and_rescue", "search and rescue": "search_and_rescue",
        "dredging": "dredging", "dredger": "dredging",
        "diving": "diving",
        "military": "military", "law enforcement": "military",
    }
    for key, val in mapping.items():
        if key in vt:
            return val
    return "other"


async def upsert_vessel(pool: asyncpg.Pool, record: Dict[str, Any]) -> int:
    """
    Insert or update a row in the `vessels` table.
    Returns the internal vessels.id (BIGSERIAL) for use as FK in child tables.

    Uses ON CONFLICT(mmsi) to update mutable fields.
    Never overwrites first_seen on conflict (keeps the earliest timestamp).
    """
    mmsi_str = str(record["mmsi"])
    vessel_type = _map_vessel_type(record.get("vessel_type_str"))
    ts = record.get("timestamp") or datetime.now(timezone.utc)
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.now(timezone.utc)

    # imo_number: store as VARCHAR
    imo_raw = record.get("imo_number")
    imo_str = str(imo_raw) if imo_raw else None

    row = await pool.fetchrow(
        """
        INSERT INTO vessels (
            mmsi, name, vessel_type, imo_number, call_sign, flag,
            draught, destination, eta, cargo_type, length_m, width_m,
            operator, gross_tonnage,
            first_seen, last_seen, last_lat, last_lon, last_course, last_heading,
            last_draught, last_draught_at
        ) VALUES ($1,$2,$3::vessel_type_enum,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)
        ON CONFLICT (mmsi) DO UPDATE SET
            name          = COALESCE(EXCLUDED.name, vessels.name),
            vessel_type   = CASE
                                WHEN EXCLUDED.vessel_type <> 'unknown'::vessel_type_enum
                                THEN EXCLUDED.vessel_type
                                ELSE vessels.vessel_type
                            END,
            imo_number    = COALESCE(EXCLUDED.imo_number, vessels.imo_number),
            call_sign     = COALESCE(EXCLUDED.call_sign, vessels.call_sign),
            flag          = COALESCE(EXCLUDED.flag, vessels.flag),
            draught       = COALESCE(EXCLUDED.draught, vessels.draught),
            destination   = COALESCE(EXCLUDED.destination, vessels.destination),
            eta           = COALESCE(EXCLUDED.eta, vessels.eta),
            cargo_type    = COALESCE(EXCLUDED.cargo_type, vessels.cargo_type),
            length_m      = COALESCE(EXCLUDED.length_m, vessels.length_m),
            width_m       = COALESCE(EXCLUDED.width_m, vessels.width_m),
            operator      = COALESCE(EXCLUDED.operator, vessels.operator),
            gross_tonnage = COALESCE(EXCLUDED.gross_tonnage, vessels.gross_tonnage),
            last_seen     = GREATEST(EXCLUDED.last_seen, vessels.last_seen),
            last_lat      = COALESCE(EXCLUDED.last_lat, vessels.last_lat),
            last_lon      = COALESCE(EXCLUDED.last_lon, vessels.last_lon),
            last_course   = COALESCE(EXCLUDED.last_course, vessels.last_course),
            last_heading  = COALESCE(EXCLUDED.last_heading, vessels.last_heading),
            last_draught  = COALESCE(EXCLUDED.last_draught, vessels.last_draught),
            last_draught_at = CASE
                WHEN EXCLUDED.last_draught IS NOT NULL THEN EXCLUDED.last_draught_at
                ELSE vessels.last_draught_at
            END
        RETURNING id
        """,
        mmsi_str,
        record.get("vessel_name") or record.get("name"),
        vessel_type,
        imo_str,
        record.get("call_sign"),
        record.get("flag"),
        record.get("draught"),
        record.get("destination"),
        record.get("eta"),
        record.get("cargo_type"),
        record.get("length_m"),
        record.get("width_m"),
        record.get("operator"),
        record.get("gross_tonnage"),
        ts,   # first_seen
        ts,   # last_seen
        record.get("lat"),
        record.get("lon"),
        record.get("course"),
        record.get("heading"),
        record.get("draught"),
        ts if record.get("draught") is not None else None,
    )
    return row["id"]


async def insert_draught_observation(pool: asyncpg.Pool, vessel_id: int, record: Dict[str, Any]) -> None:
    """Persist only genuine draught observations; absence never means a change."""
    value = record.get("draught")
    if value is None:
        return
    try:
        draught = float(value)
    except (TypeError, ValueError):
        return
    if not 0 <= draught <= 30:
        return
    timestamp = record.get("timestamp") or datetime.now(timezone.utc)
    await pool.execute(
        """INSERT INTO vessel_draught_observations (vessel_id, mmsi, timestamp, draught_m, source)
           VALUES ($1, $2, $3, $4, $5)""",
        vessel_id, str(record["mmsi"]), timestamp, draught,
        record.get("raw_source", "ais"),
    )


async def get_or_create_vessel_id(pool: asyncpg.Pool, mmsi: Any) -> Optional[int]:
    """
    Return the vessels.id for a given MMSI string/int.
    Creates a minimal vessel row if not found.
    """
    mmsi_str = str(mmsi)
    row = await pool.fetchrow("SELECT id FROM vessels WHERE mmsi=$1", mmsi_str)
    if row:
        return row["id"]
    # Create minimal row
    row = await pool.fetchrow(
        "INSERT INTO vessels (mmsi) VALUES ($1) ON CONFLICT (mmsi) DO UPDATE SET mmsi=EXCLUDED.mmsi RETURNING id",
        mmsi_str,
    )
    return row["id"] if row else None


async def insert_position(pool: asyncpg.Pool, vessel_id: int, record: Dict[str, Any]) -> None:
    """
    Insert a row into `vessel_positions` using the new schema (vessel_id FK, latitude/longitude columns).
    The geom column is auto-populated by the DB trigger.
    """
    nav_raw = record.get("nav_status")
    nav_str = _nav_status_label(nav_raw) if isinstance(nav_raw, int) else nav_raw

    await pool.execute(
        """
        INSERT INTO vessel_positions
            (vessel_id, timestamp, latitude, longitude,
             speed_knots, course_deg, heading_deg, nav_status,
             source, quality_flag)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """,
        vessel_id,
        record["timestamp"],
        float(record["lat"]),
        float(record["lon"]),
        record.get("speed_knots"),
        record.get("course"),
        record.get("heading"),
        nav_str,
        record.get("raw_source", "aisstream"),
        record.get("quality_flag", "raw"),
    )


def _nav_status_label(code: Optional[int]) -> Optional[str]:
    """Map AIS navigational status code to human-readable label."""
    _labels = {
        0: "under way using engine",
        1: "at anchor",
        2: "not under command",
        3: "restricted manoeuvrability",
        4: "constrained by draught",
        5: "moored",
        6: "aground",
        7: "engaged in fishing",
        8: "under way sailing",
        15: "undefined",
    }
    if code is None:
        return None
    return _labels.get(code, f"status_{code}")


async def fetch_vessel_last_position(pool: asyncpg.Pool, mmsi: Any) -> Optional[Dict[str, Any]]:
    """Return the most recent position record for a vessel by MMSI."""
    row = await pool.fetchrow(
        """
        SELECT vp.latitude AS lat, vp.longitude AS lon, vp.timestamp,
               vp.speed_knots, vp.heading_deg AS heading
        FROM vessel_positions vp
        JOIN vessels v ON vp.vessel_id = v.id
        WHERE v.mmsi = $1
        ORDER BY vp.timestamp DESC LIMIT 1
        """,
        str(mmsi),
    )
    return dict(row) if row else None


async def fetch_recent_positions(
    pool: asyncpg.Pool,
    mmsi: Any,
    since_ts: datetime,
) -> List[Dict[str, Any]]:
    """Fetch all positions for a vessel since a given timestamp."""
    rows = await pool.fetch(
        """
        SELECT vp.latitude AS lat, vp.longitude AS lon,
               vp.speed_knots, vp.course_deg AS course,
               vp.heading_deg AS heading, vp.timestamp, vp.quality_flag
        FROM vessel_positions vp
        JOIN vessels v ON vp.vessel_id = v.id
        WHERE v.mmsi = $1 AND vp.timestamp >= $2
        ORDER BY vp.timestamp ASC
        """,
        str(mmsi),
        since_ts,
    )
    return [dict(r) for r in rows]


async def get_all_active_vessels(
    pool: asyncpg.Pool,
    active_within_minutes: int = 30,
) -> List[Dict[str, Any]]:
    """
    Return all vessels seen within the last N minutes with last known position.
    Used by AIS Analytics for cross-vessel proximity checks.
    """
    rows = await pool.fetch(
        """
        SELECT id AS vessel_id, mmsi, vessel_type::TEXT, last_lat AS lat, last_lon AS lon, last_seen
        FROM vessels
        WHERE last_seen >= NOW() - ($1 || ' minutes')::INTERVAL
          AND last_lat IS NOT NULL
        """,
        str(active_within_minutes),
    )
    return [dict(r) for r in rows]
