"""
Load seed_incidents.json into Aqua Sentinel's existing PostGIS database.

Usage:
    python load_seed.py

The migration must be applied first. Inserts use source name and
(title, incident_date, country) as stable natural keys, so reruns are safe.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from datetime import date, time


async def load_seed(file_path: str) -> tuple[int, int]:
    import asyncpg

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        dsn = (
            f"postgresql://{os.environ.get('POSTGRES_USER', 'aqua_sentinel')}"
            f":{os.environ.get('POSTGRES_PASSWORD', 'change_me')}"
            f"@{os.environ.get('POSTGRES_HOST', 'localhost')}"
            f":{os.environ.get('POSTGRES_PORT', '5433')}"
            f"/{os.environ.get('POSTGRES_DB', 'aqua_sentinel')}"
        )
    data = json.loads(Path(file_path).read_text())
    source_aliases = {
        "src-tribune": "src-news-tribune",
        "src-deccanherald": "src-news-deccanherald",
    }
    conn = await asyncpg.connect(dsn)
    source_ids = {}
    try:
        async with conn.transaction():
            for source in data["sources"]:
                source_id = await conn.fetchval(
                    """
                    INSERT INTO sources (name, source_type, url, license, reliability_tier)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (name) DO UPDATE SET
                        source_type = EXCLUDED.source_type,
                        url = EXCLUDED.url,
                        license = EXCLUDED.license,
                        reliability_tier = EXCLUDED.reliability_tier
                    RETURNING id
                    """,
                    source["name"], source["source_type"], source.get("url"),
                    source.get("license"), source["reliability_tier"],
                )
                source_ids[source["id"]] = source_id

            for incident in data["incidents"]:
                incident_id = await conn.fetchval(
                    """
                    INSERT INTO incidents (
                        title, description, cause_description, incident_date,
                        incident_time, date_precision, geom, location_text,
                        location_precision, country, admin_region, water_body,
                        spill_type, substance_type, volume_min_liters,
                        volume_max_liters, volume_precision, area_affected_km2,
                        vessels_involved, status, response_summary,
                        confidence_level, is_synthetic, synthetic_notes
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6,
                        ST_SetSRID(ST_MakePoint($7, $8), 4326), $9, $10, $11,
                        $12, $13, $14, $15, $16, $17, $18, $19, $20::jsonb,
                        $21, $22, $23, $24, $25
                    )
                    ON CONFLICT (title, incident_date, country) DO UPDATE SET
                        description = EXCLUDED.description,
                        cause_description = EXCLUDED.cause_description,
                        incident_time = EXCLUDED.incident_time,
                        date_precision = EXCLUDED.date_precision,
                        geom = EXCLUDED.geom,
                        location_text = EXCLUDED.location_text,
                        location_precision = EXCLUDED.location_precision,
                        admin_region = EXCLUDED.admin_region,
                        water_body = EXCLUDED.water_body,
                        spill_type = EXCLUDED.spill_type,
                        substance_type = EXCLUDED.substance_type,
                        volume_min_liters = EXCLUDED.volume_min_liters,
                        volume_max_liters = EXCLUDED.volume_max_liters,
                        volume_precision = EXCLUDED.volume_precision,
                        area_affected_km2 = EXCLUDED.area_affected_km2,
                        vessels_involved = EXCLUDED.vessels_involved,
                        status = EXCLUDED.status,
                        response_summary = EXCLUDED.response_summary,
                        confidence_level = EXCLUDED.confidence_level,
                        is_synthetic = EXCLUDED.is_synthetic,
                        synthetic_notes = EXCLUDED.synthetic_notes,
                        updated_at = now()
                    RETURNING id
                    """,
                    incident["title"], incident.get("description"), incident.get("cause_description"),
                    date.fromisoformat(incident["incident_date"]), time.fromisoformat(incident["incident_time"]) if incident.get("incident_time") else None, incident.get("date_precision", "day"),
                    incident["longitude"], incident["latitude"], incident.get("location_text"),
                    incident.get("location_precision", "approximate"), incident["country"],
                    incident.get("admin_region"), incident.get("water_body"), incident.get("spill_type", "unknown"),
                    incident.get("substance_type"), incident.get("volume_min_liters"), incident.get("volume_max_liters"),
                    incident.get("volume_precision", "unknown"), incident.get("area_affected_km2"),
                    json.dumps(incident.get("vessels_involved", [])), incident.get("status", "confirmed"),
                    incident.get("response_summary"), incident["confidence_level"],
                    incident.get("is_synthetic", False), incident.get("synthetic_notes"),
                )
                for source_key in incident.get("sources", []):
                    source_key = source_aliases.get(source_key, source_key)
                    await conn.execute(
                        """
                        INSERT INTO incident_sources (incident_id, source_id)
                        VALUES ($1, $2) ON CONFLICT (incident_id, source_id) DO NOTHING
                        """,
                        incident_id, source_ids[source_key],
                    )
        return len(data["sources"]), len(data["incidents"])
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--file",
        default=str(Path(__file__).parent / "seed_incidents.json"),
        help="Path to seed JSON file",
    )
    args = parser.parse_args()
    sources, incidents = asyncio.run(load_seed(args.file))
    print(f"Loaded {sources} sources and {incidents} incidents.")


if __name__ == "__main__":
    main()
