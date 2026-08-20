-- Safe to run repeatedly on existing developer databases.
-- Keep this aligned with infra/postgres/init.sql because Docker's init scripts
-- run only when the Postgres volume is first created.

-- Step 7 synthetic-demo provenance. Existing rows default to FALSE: real
-- observations are never rewritten as synthetic.
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS is_synthetic BOOLEAN NOT NULL DEFAULT FALSE;