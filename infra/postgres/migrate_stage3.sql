-- Safe to run repeatedly on existing developer databases.
-- Keep this aligned with infra/postgres/init.sql because Docker's init scripts
-- run only when the Postgres volume is first created.

-- Step 4 lookalike-engine classification labels. 'raw' is preserved: existing
-- spill_candidates rows keep their status. No additional statuses are invented
-- and none are removed.
DO $$ BEGIN
    ALTER TYPE spill_candidate_status_enum ADD VALUE IF NOT EXISTS 'likely_ship_shadow';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TYPE spill_candidate_status_enum ADD VALUE IF NOT EXISTS 'likely_calm_water';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TYPE spill_candidate_status_enum ADD VALUE IF NOT EXISTS 'possible_slick';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;