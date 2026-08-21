-- Migration 001 — satellite_tasking_requests.scene_id / completed_at
--
-- For EXISTING pgdata volumes (init.sql only runs on first boot):
--   docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     < infra/postgres/migrations/001_tasking_scene_id.sql
--
-- All statements are idempotent; safe to run repeatedly.

ALTER TABLE satellite_tasking_requests ADD COLUMN IF NOT EXISTS scene_id     VARCHAR(255);
ALTER TABLE satellite_tasking_requests ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_tasking_status_requested ON satellite_tasking_requests (status, requested_at);
CREATE INDEX IF NOT EXISTS idx_dark_vessel_detected_at ON dark_vessel_events (detected_at);
