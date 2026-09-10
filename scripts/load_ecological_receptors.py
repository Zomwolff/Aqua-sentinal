"""
load_ecological_receptors.py
============================
Phase 2: Load preprocessed ecological receptor GeoJSON files into the
PostGIS ecological_receptors table.

Prerequisites:
    1. migrations/010_ecological_receptors.sql has been applied to the database.
  2. acquire_ecological_data.py has been run and produced the processed
     GeoJSON files in data/ecological/processed/.

IDEMPOTENT: Safe to re-run. Uses a delete-then-insert strategy per
receptor_type so rerunning does not create uncontrolled duplicates.
Each receptor_type is replaced atomically within a single transaction.

SCOPE (V1):
  mangrove           ← data/ecological/processed/gmw/
  coral_reef         ← data/ecological/processed/aca/
  mpa                ← data/ecological/processed/wdpa/
  sensitive_coastline ← data/ecological/processed/coastline/ (Phase 3)

USAGE:
  python scripts/load_ecological_receptors.py [--types mangrove coral mpa]
  python scripts/load_ecological_receptors.py          # all V1 types
  python scripts/load_ecological_receptors.py --dry-run

DO NOT implement ecological scoring here.
DO NOT modify protected_areas.
DO NOT query Oil Spread forecasts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import geopandas as gpd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("load_ecological")

# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).parent.parent
PROC_DIR   = _REPO_ROOT / "data" / "ecological" / "processed"

# Processed GeoJSON files produced by acquire_ecological_data.py (Phase 2)
# and classify_sensitive_coastline.py (Phase 3)
SOURCES = {
    "mangrove": PROC_DIR / "gmw" / "gmw_v3_2020_mangroves_aoi.geojson",
    "coral_reef": PROC_DIR / "aca" / "coral_reefs_aoi.geojson",
    "mpa": PROC_DIR / "wdpa" / "wdpa_mpas_india_aoi.geojson",
    "sensitive_coastline": PROC_DIR / "coastline" / "sensitive_coastline_classified.geojson",  # Phase 3
}

# Sensitivity tiers [PROJECT-DEFINED]
# Phase 2: mangrove, coral_reef, mpa → fixed high tier
# Phase 3: sensitive_coastline → tiers (high/medium/low) from classification, read from GeoJSON
SENSITIVITY = {
    "mangrove":   "high",
    "coral_reef": "high",
    "mpa":        "high",
    "sensitive_coastline": None,  # Read from GeoJSON feature properties
}

PROTECTION = {
    "mangrove":   False,
    "coral_reef": False,
    "mpa":        True,
    "sensitive_coastline": False,
}


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

async def _get_pool():
    """
    Create asyncpg pool.
    Uses the project shared DB connection utility, which reads credentials
    from environment variables (POSTGRES_HOST, POSTGRES_USER, etc.) or DATABASE_URL.
    For local dev outside Docker the .env values take precedence if set.
    """
    sys.path.insert(0, str(_REPO_ROOT))

    # Load .env into the environment so shared.db.connection picks up real creds.
    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k not in os.environ:  # don't override real env
                    os.environ[k] = v

    from shared.db.connection import create_pool
    return await create_pool(min_size=1, max_size=3)


# ─────────────────────────────────────────────────────────────────────────────
# METADATA EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def _build_metadata(row: Any, receptor_type: str) -> dict:
    """Extract any useful original attributes into the metadata JSONB field."""
    meta: dict = {}
    row_dict = dict(row) if hasattr(row, "items") else {}
    skip = {
        "geometry", "receptor_type", "source", "source_year",
        "sensitivity_tier", "protection_status", "name",
    }
    for k, v in row_dict.items():
        if k in skip:
            continue
        if v is None:
            continue
        try:
            # Ensure JSON-serialisable
            json.dumps(v)
            meta[k] = v
        except (TypeError, ValueError):
            meta[k] = str(v)
    return meta


# ─────────────────────────────────────────────────────────────────────────────
# LOADER
# ─────────────────────────────────────────────────────────────────────────────

async def load_receptor_type(
    pool,
    receptor_type: str,
    geojson_path: Path,
    dry_run: bool = False,
) -> dict:
    """
    Atomically replace all rows for receptor_type with data from geojson_path.
    Returns a stats dict.
    """
    if not geojson_path.exists():
        raise FileNotFoundError(
            f"Processed file not found: {geojson_path}\n"
            f"Run acquire_ecological_data.py first."
        )

    log.info("Loading %s from %s …", receptor_type, geojson_path.name)
    gdf = gpd.read_file(geojson_path)

    if len(gdf) == 0:
        log.warning("%s: GeoJSON has 0 features — skipping DB insert", receptor_type)
        return dict(receptor_type=receptor_type, inserted=0, deleted=0, skipped=0)

    # Verify CRS
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        log.warning("%s: unexpected CRS %s — reprojecting to EPSG:4326", receptor_type, gdf.crs)
        gdf = gdf.to_crs("EPSG:4326")

    # Count invalid geometries
    n_invalid = int((~gdf.geometry.is_valid).sum())
    n_empty   = int(gdf.geometry.is_empty.sum())
    if n_invalid > 0:
        log.warning("%s: %d invalid geometries — will use ST_MakeValid in PostGIS", receptor_type, n_invalid)
    if n_empty > 0:
        log.warning("%s: %d empty geometries — will be skipped", receptor_type, n_empty)

    rows_to_insert = []
    skipped = 0
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            skipped += 1
            continue

        # Convert geometry to WKT for PostGIS insertion
        wkt = geom.wkt

        # Name: try common name columns
        name_val = None
        for col in ("name", "NAME", "wdpa_id", "designation_eng"):
            v = row.get(col) if hasattr(row, "get") else getattr(row, col, None)
            if v and str(v).strip():
                name_val = str(v).strip()
                break

        # Sensitivity tier: read from GeoJSON for sensitive_coastline, use fixed value otherwise
        if receptor_type == "sensitive_coastline":
            # Phase 3: tiers are classified and stored in the GeoJSON
            tier_from_row = row.get("sensitivity_tier") if hasattr(row, "get") else None
            if tier_from_row not in ("low", "medium", "high"):
                log.warning("%s: invalid sensitivity_tier '%s' for feature %s — defaulting to 'low'",
                           receptor_type, tier_from_row, name_val)
                tier_from_row = "low"
            sensitivity_tier = tier_from_row
        else:
            sensitivity_tier = SENSITIVITY[receptor_type]
        
        meta = _build_metadata(row, receptor_type)
        rows_to_insert.append((
            receptor_type,
            name_val,
            wkt,
            str(row.get("source", SOURCES[receptor_type].name) if hasattr(row,"get") else receptor_type),
            int(row.get("source_year", 0) or 0) or None,
            sensitivity_tier,
            PROTECTION[receptor_type],
            json.dumps(meta) if meta else None,
        ))

    n_to_insert = len(rows_to_insert)
    log.info("%s: %d features to insert (%d skipped empty)",
             receptor_type, n_to_insert, skipped)

    if dry_run:
        log.info("[DRY RUN] Would delete existing %s rows and insert %d",
                 receptor_type, n_to_insert)
        return dict(receptor_type=receptor_type, inserted=0, deleted=0,
                    skipped=skipped, dry_run=True)

    if n_to_insert == 0:
        log.warning("%s: nothing to insert after filtering — no DB changes made", receptor_type)
        return dict(receptor_type=receptor_type, inserted=0, deleted=0, skipped=skipped)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Idempotent: delete existing rows for this type first
            result = await conn.execute(
                "DELETE FROM ecological_receptors WHERE receptor_type = $1",
                receptor_type,
            )
            # asyncpg execute() returns a status string like "DELETE 42"
            try:
                deleted = int(result.split()[-1])
            except (ValueError, IndexError):
                deleted = 0

            inserted = 0
            for (rtype, name, wkt, source, source_year,
                 sens_tier, prot_status, metadata) in rows_to_insert:
                try:
                    await conn.execute(
                        """
                        INSERT INTO ecological_receptors
                            (receptor_type, name, geom, source, source_year,
                             sensitivity_tier, protection_status, metadata)
                        VALUES (
                            $1, $2,
                            ST_Multi(ST_MakeValid(
                                ST_SetSRID(ST_GeomFromText($3), 4326)
                            )),
                            $4, $5, $6, $7, $8::jsonb
                        )
                        """,
                        rtype, name, wkt, source, source_year,
                        sens_tier, prot_status, metadata,
                    )
                    inserted += 1
                except Exception as exc:
                    log.warning("%s: insert failed for one feature: %s", receptor_type, exc)
                    skipped += 1

    log.info("%s: deleted=%d  inserted=%d  skipped=%d",
             receptor_type, deleted, inserted, skipped)
    return dict(receptor_type=receptor_type, inserted=inserted,
                deleted=deleted, skipped=skipped)


async def verify_load(pool) -> None:
    """Print a post-load QA summary from the database."""
    log.info("=" * 55)
    log.info("POST-LOAD VERIFICATION")
    log.info("=" * 55)
    async with pool.acquire() as conn:
        # Row counts by type
        rows = await conn.fetch(
            """
            SELECT receptor_type,
                   COUNT(*)                                       AS n_features,
                   SUM(CASE WHEN NOT ST_IsValid(geom) THEN 1 ELSE 0 END) AS n_invalid,
                   SUM(CASE WHEN ST_IsEmpty(geom)     THEN 1 ELSE 0 END) AS n_empty,
                   MIN(ST_YMin(geom::geometry))                   AS min_lat,
                   MAX(ST_YMax(geom::geometry))                   AS max_lat,
                   MIN(ST_XMin(geom::geometry))                   AS min_lon,
                   MAX(ST_XMax(geom::geometry))                   AS max_lon,
                   ROUND(SUM(ST_Area(geom::geography))::numeric / 1e6, 2) AS total_area_km2
            FROM ecological_receptors
            GROUP BY receptor_type
            ORDER BY receptor_type
            """
        )
        for r in rows:
            log.info(
                "  %-22s  n=%5d  invalid=%d  empty=%d  "
                "lat=[%.2f,%.2f]  lon=[%.2f,%.2f]  area_km2=%s",
                r["receptor_type"], r["n_features"],
                r["n_invalid"], r["n_empty"],
                float(r["min_lat"] or 0), float(r["max_lat"] or 0),
                float(r["min_lon"] or 0), float(r["max_lon"] or 0),
                r["total_area_km2"],
            )

        # SRID check
        srid_row = await conn.fetchrow(
            """
            SELECT DISTINCT ST_SRID(geom) AS srid
            FROM ecological_receptors
            LIMIT 1
            """
        )
        if srid_row:
            srid = srid_row["srid"]
            status = "OK" if srid == 4326 else f"WRONG ({srid})"
            log.info("  SRID check: %s", status)

        # GIST index check
        idx_row = await conn.fetchrow(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'ecological_receptors'
              AND indexname = 'idx_ecological_receptors_geom'
            """
        )
        log.info("  GIST index idx_ecological_receptors_geom: %s",
                 "present" if idx_row else "MISSING")

        # Sample geometry in AOI
        sample = await conn.fetchrow(
            """
            SELECT receptor_type, name,
                   ST_YMin(geom::geometry) AS lat,
                   ST_XMin(geom::geometry) AS lon
            FROM ecological_receptors
            WHERE ST_Intersects(
                geom,
                ST_MakeEnvelope(68, 14, 77.5, 25, 4326)
            )
            LIMIT 1
            """
        )
        if sample:
            log.info(
                "  Sample in core AOI: type=%s  name=%s  approx_lat=%.3f  approx_lon=%.3f",
                sample["receptor_type"], sample["name"],
                float(sample["lat"]), float(sample["lon"]),
            )
        else:
            log.warning("  No features found within core AOI 68-77.5E, 14-25N — check data!")

    log.info("=" * 55)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

ALL_TYPES = list(SOURCES.keys())  # mangrove, coral_reef, mpa


async def _main(types: list[str], dry_run: bool) -> None:
    pool = await _get_pool()
    try:
        results = []
        for receptor_type in types:
            path = SOURCES[receptor_type]
            result = await load_receptor_type(pool, receptor_type, path, dry_run=dry_run)
            results.append(result)

        log.info("=" * 55)
        log.info("LOAD SUMMARY")
        log.info("=" * 55)
        for r in results:
            log.info("  %-22s  inserted=%d  deleted=%d  skipped=%d",
                     r["receptor_type"], r.get("inserted", 0),
                     r.get("deleted", 0), r.get("skipped", 0))

        if not dry_run:
            await verify_load(pool)
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load ecological receptor GeoJSON files into PostGIS"
    )
    parser.add_argument(
        "--types", nargs="+",
        choices=ALL_TYPES,
        default=ALL_TYPES,
        help="Receptor types to load (default: all V1 types)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and validate files but do not write to the database",
    )
    args = parser.parse_args()

    log.info("Receptor types: %s", args.types)
    log.info("Dry run: %s", args.dry_run)
    asyncio.run(_main(args.types, args.dry_run))


if __name__ == "__main__":
    main()
