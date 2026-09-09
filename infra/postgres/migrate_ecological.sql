-- =============================================================================
-- Aqua-Sentinel — Ecological Impact V1 Migration
-- File:    infra/postgres/migrate_ecological.sql
-- Phase:   2 (Data Acquisition & PostGIS Setup)
-- Created: 2026-09-06
--
-- WHAT THIS CREATES:
--   ecological_receptors  — unified table for all four V1 receptor types
--
-- WHAT THIS DOES NOT TOUCH:
--   protected_areas       — existing table, not modified
--   severity              — existing table, not modified
--   forecasts             — existing table, not modified
--   Any other existing table
--
-- IDEMPOTENT: Safe to run multiple times on an existing database.
--   All statements use IF NOT EXISTS / DO $$ ... EXCEPTION WHEN ... END $$.
--
-- APPLY TO EXISTING VOLUME:
--   docker compose exec -T postgres psql \
--     -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     < infra/postgres/migrate_ecological.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. ecological_receptors
--    Unified table for V1 receptor scope:
--      mangrove | coral_reef | sensitive_coastline | mpa
--
--    GEOMETRY TYPE: GEOMETRY(MultiPolygon, 4326)
--      Matches protected_areas convention. All source datasets are
--      normalised to MultiPolygon by the preprocessing script.
--      Note: the sensitive_coastline layer will also be stored as
--      MultiPolygon (buffered segments), not raw LineStrings.
--
--    SENSITIVITY TIER: 'low' | 'medium' | 'high'
--      Set during loading. Values are project-defined operational
--      assignments, not universally validated scientific constants.
--      See scripts/load_ecological_receptors.py for rationale.
--
--    SOURCE: free-text reference to the dataset (e.g. "GMW v3.0 2020")
--
--    METADATA: JSONB for any additional receptor-specific attributes
--      (e.g. WDPA ID, IUCN category, marine status, area_km2)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ecological_receptors (
    id               BIGSERIAL PRIMARY KEY,

    -- Receptor classification (fixed V1 vocabulary)
    receptor_type    TEXT NOT NULL CHECK (
                         receptor_type IN (
                             'mangrove',
                             'coral_reef',
                             'sensitive_coastline',
                             'mpa'
                         )
                     ),

    -- Human-readable name (PA name for MPAs; NULL for habitat polygons)
    name             TEXT,

    -- Geometry: always MultiPolygon, always EPSG:4326
    -- For metric area calculations: ST_Area(geom::geography) → m²
    -- For topology (intersection): ST_Intersects(geom, other_geom) — geometry, no cast
    -- For intersection area:        ST_Area(ST_Intersection(geom, other)::geography) → m²
    geom             GEOMETRY(MultiPolygon, 4326) NOT NULL,

    -- Provenance
    source           TEXT NOT NULL,          -- dataset name + version
    source_year      INTEGER,                -- data epoch year

    -- Sensitivity tier [PROJECT-DEFINED — not a universal scientific constant]
    -- high:   mangrove, coral_reef, MPA
    -- medium: assigned during Phase 3 sensitive-coastline classification
    -- low:    lower-sensitivity coastline segments (Phase 3)
    sensitivity_tier TEXT CHECK (
                         sensitivity_tier IN ('low', 'medium', 'high')
                     ),

    -- Whether the receptor has formal legal protection (true for MPAs)
    protection_status BOOLEAN DEFAULT FALSE,

    -- Flexible extra attributes (WDPA ID, IUCN category, area, etc.)
    metadata          JSONB,

    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 2. Spatial index — mandatory for ST_Intersects / ST_Intersection queries
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_ecological_receptors_geom
    ON ecological_receptors
    USING GIST (geom);

-- ---------------------------------------------------------------------------
-- 3. Receptor-type index — for filtered intersection queries
--    (e.g. "intersect forecast only with mangroves")
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_ecological_receptors_type
    ON ecological_receptors (receptor_type);

-- ---------------------------------------------------------------------------
-- 4. Composite index for the most common query pattern:
--    type + spatial filter
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_ecological_receptors_type_geom
    ON ecological_receptors (receptor_type)
    INCLUDE (id, name, sensitivity_tier, protection_status);

-- ---------------------------------------------------------------------------
-- 5. Comments documenting spatial conventions
-- ---------------------------------------------------------------------------

COMMENT ON TABLE ecological_receptors IS
    'Unified ecological receptor layer for oil-spill exposure assessment. '
    'V1 scope: mangrove, coral_reef, sensitive_coastline, mpa. '
    'Geometry is always GEOMETRY(MultiPolygon,4326). '
    'For metric area: ST_Area(geom::geography). '
    'For intersection: ST_Intersects(geom, forecast.geom) stays geometry. '
    'For intersection area: ST_Area(ST_Intersection(geom, forecast.geom)::geography).';

COMMENT ON COLUMN ecological_receptors.sensitivity_tier IS
    '[PROJECT-DEFINED] Operational sensitivity assignment: high/medium/low. '
    'Not a universal scientific constant. '
    'mangrove=high, coral_reef=high, mpa=high (Phase 2). '
    'sensitive_coastline tiers assigned in Phase 3.';

COMMENT ON COLUMN ecological_receptors.geom IS
    'MultiPolygon, EPSG:4326. '
    'Use ::geography cast for metric distance/area calculations. '
    'Use geometry (no cast) for topology operations.';

COMMENT ON COLUMN ecological_receptors.source IS
    'Free-text source reference, e.g. "GMW v3.0 2020 (Zenodo 6894273)". '
    'Full provenance in data/ecological/manifest/manifest.json.';

-- ---------------------------------------------------------------------------
-- 6. Verify migration applied (informational only — no side effects)
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    tbl_exists  BOOLEAN;
    idx1_exists BOOLEAN;
    idx2_exists BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'ecological_receptors'
    ) INTO tbl_exists;

    SELECT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'ecological_receptors'
          AND indexname = 'idx_ecological_receptors_geom'
    ) INTO idx1_exists;

    SELECT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'ecological_receptors'
          AND indexname = 'idx_ecological_receptors_type'
    ) INTO idx2_exists;

    IF tbl_exists AND idx1_exists AND idx2_exists THEN
        RAISE NOTICE 'migrate_ecological.sql: ecological_receptors OK (table + indexes present)';
    ELSE
        RAISE EXCEPTION 'migrate_ecological.sql: verification failed — '
            'table_exists=%, gist_index=%, type_index=%',
            tbl_exists, idx1_exists, idx2_exists;
    END IF;
END $$;
