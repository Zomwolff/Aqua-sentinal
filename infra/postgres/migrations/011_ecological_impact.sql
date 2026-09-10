-- =============================================================================
-- Aqua-Sentinel — Ecological Impact V1 Migration
-- File:    infra/postgres/migrate_ecological_impact.sql
-- Phase:   4 (Ecological Impact Calculation & Backend Service)
-- Created: 2026-09-06
--
-- WHAT THIS CREATES:
--   ecological_impact  — ecological exposure results per spill/horizon/receptor
--
-- IDEMPOTENT: Safe to run multiple times on an existing database.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. ecological_impact
--    Stores ecological exposure calculations for each spill forecast.
--    One row per (spill_id, horizon_hours, footprint_type, receptor_type).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ecological_impact (
    id                         BIGSERIAL PRIMARY KEY,
    
    -- Spill and forecast identification
    spill_id                   UUID NOT NULL REFERENCES spill_incidents (id) ON DELETE CASCADE,
    horizon_hours              NUMERIC(6,2) NOT NULL,
    footprint_type             TEXT NOT NULL CHECK (footprint_type IN ('best_estimate', 'probability_90')),
    receptor_type              TEXT NOT NULL CHECK (receptor_type IN ('mangrove', 'coral_reef', 'mpa', 'sensitive_coastline')),
    
    -- Area metrics (all in km²)
    overlap_area_km2           NUMERIC(14,4) NOT NULL CHECK (overlap_area_km2 >= 0),
    receptor_area_km2          NUMERIC(14,4) NOT NULL CHECK (receptor_area_km2 >= 0),
    spill_area_km2             NUMERIC(14,4) NOT NULL CHECK (spill_area_km2 >= 0),
    
    -- Primary exposure metric [PROJECT-DEFINED]
    exposure_pct               NUMERIC(7,4) NOT NULL CHECK (exposure_pct BETWEEN 0 AND 100),
    
    -- Contextual metric (what % of spill overlaps receptor)
    spill_share_pct            NUMERIC(7,4) NOT NULL CHECK (spill_share_pct BETWEEN 0 AND 100),
    
    -- Exposure category [PROJECT-DEFINED thresholds: 0, 10, 30, 60]
    category                   TEXT NOT NULL CHECK (category IN ('None', 'Low', 'Medium', 'High', 'Critical')),
    
    -- Receptor characteristics (from ecological_receptors, not weighted)
    sensitivity_tier           TEXT CHECK (sensitivity_tier IN ('low', 'medium', 'high')),
    protection_status          BOOLEAN,
    
    -- Temporal urgency indicator
    time_to_first_exposure_hours NUMERIC(6,2),
    
    -- Metadata
    metadata                   JSONB,  -- classification_reason, fallback flags, etc.
    computed_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Uniqueness: one result per (spill, horizon, footprint, receptor_type)
    UNIQUE (spill_id, horizon_hours, footprint_type, receptor_type)
);

-- ---------------------------------------------------------------------------
-- 2. Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_ecological_impact_spill_id
    ON ecological_impact (spill_id);

CREATE INDEX IF NOT EXISTS idx_ecological_impact_horizon
    ON ecological_impact (horizon_hours);

CREATE INDEX IF NOT EXISTS idx_ecological_impact_receptor_type
    ON ecological_impact (receptor_type);

CREATE INDEX IF NOT EXISTS idx_ecological_impact_category
    ON ecological_impact (category);

CREATE INDEX IF NOT EXISTS idx_ecological_impact_exposure_pct
    ON ecological_impact (exposure_pct DESC);

-- Composite index for common query pattern: spill + horizon lookup
CREATE INDEX IF NOT EXISTS idx_ecological_impact_spill_horizon
    ON ecological_impact (spill_id, horizon_hours);

-- ---------------------------------------------------------------------------
-- 3. Comments documenting methodology
-- ---------------------------------------------------------------------------

COMMENT ON TABLE ecological_impact IS
    'Ecological exposure results for oil spill forecasts. '
    'One row per (spill_id, horizon_hours, footprint_type, receptor_type). '
    'Exposure percentage is [PROJECT-DEFINED] as (overlap_area / receptor_area) * 100. '
    'Category thresholds: None=0%, Low=0-10%, Medium=10-30%, High=30-60%, Critical=60%+. '
    'Does NOT combine receptor types into a single ecological score.';

COMMENT ON COLUMN ecological_impact.exposure_pct IS
    '[PROJECT-DEFINED] Primary ecological exposure metric: '
    '(intersection_area / receptor_area) * 100. '
    'Aggregated across all receptor geometries of the same type.';

COMMENT ON COLUMN ecological_impact.spill_share_pct IS
    '[CONTEXTUAL] What percentage of the spill footprint overlaps this receptor: '
    '(intersection_area / spill_area) * 100. '
    'Does NOT affect category classification.';

COMMENT ON COLUMN ecological_impact.category IS
    '[PROJECT-DEFINED] Exposure category based on exposure_pct thresholds: '
    'None: 0%, Low: >0-10%, Medium: >10-30%, High: >30-60%, Critical: >60%. '
    'These are operational thresholds for V1, not universal scientific limits.';

COMMENT ON COLUMN ecological_impact.footprint_type IS
    'best_estimate: uses forecasts.geom (50% probability contour). '
    'probability_90: uses forecasts.probability_90_geom (90% probability contour). '
    'If probability_90_geom is NULL, falls back to best_estimate with metadata flag.';

COMMENT ON COLUMN ecological_impact.time_to_first_exposure_hours IS
    'Earliest forecast horizon where exposure_pct > 0 for this receptor type. '
    'Temporal urgency indicator. NULL if no exposure at any horizon.';

-- ---------------------------------------------------------------------------
-- 4. Verify migration applied
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    tbl_exists  BOOLEAN;
    idx1_exists BOOLEAN;
    idx2_exists BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'ecological_impact'
    ) INTO tbl_exists;

    SELECT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'ecological_impact'
          AND indexname = 'idx_ecological_impact_spill_id'
    ) INTO idx1_exists;

    SELECT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'ecological_impact'
          AND indexname = 'idx_ecological_impact_receptor_type'
    ) INTO idx2_exists;

    IF tbl_exists AND idx1_exists AND idx2_exists THEN
        RAISE NOTICE 'migrate_ecological_impact.sql: ecological_impact OK (table + indexes present)';
    ELSE
        RAISE EXCEPTION 'migrate_ecological_impact.sql: verification failed — '
            'table_exists=%, spill_id_index=%, receptor_type_index=%',
            tbl_exists, idx1_exists, idx2_exists;
    END IF;
END $$;
