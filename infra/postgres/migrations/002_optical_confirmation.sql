-- 002_optical_confirmation.sql
--
-- Adds Sentinel-2 optical secondary-verification fields to spill_candidates.
-- Populated asynchronously by the new `eo` service (see docker/eo/), which
-- consumes `spill.candidates.filtered`, attempts a cloud-filtered Sentinel-2
-- fetch for the candidate's AOI/time window, and runs the 10-band CNN
-- (optical imagery/sentinel2_cnn_10band_best.pth) when a cloud-free scene is
-- available.
--
-- These columns are intentionally nullable and update-in-place (no history
-- table): optical confirmation is best-effort, non-blocking, secondary
-- evidence per the project's own design note ("don't make Sentinel-2
-- mandatory because optical imagery can be blocked by clouds"). A NULL
-- optical_checked_at means "not attempted or not yet resolved" — it is
-- never treated as a negative signal by evidence-fusion.

ALTER TABLE spill_candidates
    ADD COLUMN IF NOT EXISTS optical_oil_probability  DOUBLE PRECISION
        CHECK (optical_oil_probability IS NULL OR optical_oil_probability BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS optical_predicted_class   VARCHAR(64),
    ADD COLUMN IF NOT EXISTS optical_class_probabilities JSONB,
    ADD COLUMN IF NOT EXISTS optical_scene_id          VARCHAR(255),
    ADD COLUMN IF NOT EXISTS optical_cloud_free        BOOLEAN,
    ADD COLUMN IF NOT EXISTS optical_checked_at        TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_spill_candidates_optical_checked
    ON spill_candidates (optical_checked_at)
    WHERE optical_checked_at IS NOT NULL;
