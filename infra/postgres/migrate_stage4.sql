-- Safe to run repeatedly on existing developer databases.
-- Keep this aligned with infra/postgres/init.sql because Docker's init scripts
-- run only when the Postgres volume is first created.

-- Step 5 scoring labels. 'raw' and the Step 4 labels are preserved; no
-- statuses are removed. These are the only additions.
DO $$ BEGIN
    ALTER TYPE spill_candidate_status_enum ADD VALUE IF NOT EXISTS 'possible_oil_spill';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TYPE spill_candidate_status_enum ADD VALUE IF NOT EXISTS 'low_confidence';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Step 5 per-candidate scoring fields.
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1);
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS classification_label spill_candidate_status_enum;
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS texture_features JSONB;