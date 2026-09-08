-- B2 Look-Alike Classifier feature columns on spill_candidates.
-- Safe to run repeatedly on existing developer databases. Additive only:
-- every new column is NULLABLE so existing rows are untouched, and no
-- existing column, constraint, index, or API view is altered.
-- Keep aligned with infra/postgres/init.sql (Docker init scripts run only
-- when the Postgres volume is first created).
--
-- Provenance of each column:
--   mean/std_backscatter : services/lookalike-engine/app/texture.py
--   glcm_*               : authoritative B2 source is
--                          sar-LinkNet-ResNet34/geo_postprocess.py
--                          (fixed [-30,0] dB, distances [1,2], mean aggregates)
--   perimeter/elongation/irregularity/edge : geo_postprocess.py B2 helpers
--                          (metric meters; irregularity = P^2/(4*pi*A))
--   wind/vessel/persistence: contextual evidence, NULL when absent
--                          (missing must stay missing, never 0).

ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS mean_backscatter DOUBLE PRECISION;
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS std_backscatter DOUBLE PRECISION;

ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS glcm_contrast DOUBLE PRECISION;
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS glcm_homogeneity DOUBLE PRECISION;
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS glcm_energy DOUBLE PRECISION;
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS glcm_correlation DOUBLE PRECISION;

ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS perimeter_m DOUBLE PRECISION
    CHECK (perimeter_m IS NULL OR perimeter_m > 0);
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS elongation DOUBLE PRECISION
    CHECK (elongation IS NULL OR elongation >= 0);
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS boundary_irregularity DOUBLE PRECISION
    CHECK (boundary_irregularity IS NULL OR boundary_irregularity > 0);
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS edge_sharpness DOUBLE PRECISION;

ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS wind_speed_kmh DOUBLE PRECISION;
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS distance_to_nearest_vessel_km DOUBLE PRECISION
    CHECK (distance_to_nearest_vessel_km IS NULL OR distance_to_nearest_vessel_km >= 0);
ALTER TABLE spill_candidates ADD COLUMN IF NOT EXISTS persistence_count INTEGER
    CHECK (persistence_count IS NULL OR persistence_count >= 1);
