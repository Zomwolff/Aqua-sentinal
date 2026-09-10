-- Safe to run repeatedly on existing developer databases.
-- Keep this aligned with infra/postgres/init.sql because Docker's init scripts
-- run only when the Postgres volume is first created.

DO $$ BEGIN
    CREATE TYPE spill_candidate_status_enum AS ENUM ('raw');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS spill_candidates (
    candidate_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scene_id         VARCHAR(255) NOT NULL,
    acquisition_time TIMESTAMPTZ NOT NULL,
    geom             GEOMETRY(Polygon, 4326) NOT NULL,
    area_m2          NUMERIC(14,2) NOT NULL CHECK (area_m2 >= 0),
    pixel_count      INTEGER NOT NULL CHECK (pixel_count > 0),
    status           spill_candidate_status_enum NOT NULL DEFAULT 'raw',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_spill_candidates_geom        ON spill_candidates USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_spill_candidates_scene_id    ON spill_candidates (scene_id);
CREATE INDEX IF NOT EXISTS idx_spill_candidates_acquisition ON spill_candidates (acquisition_time);
CREATE INDEX IF NOT EXISTS idx_spill_candidates_status      ON spill_candidates (status);