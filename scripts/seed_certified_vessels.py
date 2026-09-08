"""Attach recently active AIS vessels to demo response contractors."""
import asyncio
import os

import asyncpg


async def seed() -> None:
    conn = await asyncpg.connect(
        user=os.environ.get("POSTGRES_USER", "aqua_sentinel"),
        password=os.environ.get("POSTGRES_PASSWORD", "change_me"),
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        database=os.environ.get("POSTGRES_DB", "aqua_sentinel"),
    )
    try:
        rows = await conn.fetch(
            """
            SELECT mmsi FROM vessels
            WHERE last_lat IS NOT NULL AND last_lon IS NOT NULL
            ORDER BY last_seen DESC NULLS LAST LIMIT 5
            """
        )
        contractor = await conn.fetchrow(
            "SELECT id FROM contractors ORDER BY id LIMIT 1"
        )
        if not rows or contractor is None:
            print("No active AIS vessels or contractors found; run the migration first.")
            return
        for row in rows:
            await conn.execute(
                """
                INSERT INTO certified_vessels
                    (mmsi, contractor_id, tier_rating, equipment_summary)
                VALUES ($1, $2, 1, $3)
                ON CONFLICT (mmsi) DO UPDATE SET
                    contractor_id = EXCLUDED.contractor_id,
                    equipment_summary = EXCLUDED.equipment_summary
                """,
                str(row["mmsi"]), contractor["id"],
                "Boom + skimmer, 200T recovery capacity",
            )
        print(f"Seeded {len(rows)} certified AIS vessel(s).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed())
