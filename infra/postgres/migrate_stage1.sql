-- Safe to run repeatedly on existing developer databases.
-- Keep this aligned with infra/postgres/init.sql because Docker's init scripts
-- run only when the Postgres volume is first created.
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS max_speed_delta_kn DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS max_speed_drop_kn DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS mean_cog_heading_divergence_deg DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS max_cog_heading_divergence_deg DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS repeated_cog_heading_divergences INTEGER NOT NULL DEFAULT 0;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS max_rate_of_turn_deg_min DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS turn_reversal_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS draught_m DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS draught_change_m DOUBLE PRECISION;

ALTER TABLE anomaly_events ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION;
ALTER TABLE anomaly_events ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;
ALTER TABLE anomaly_events ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION
    CHECK (confidence_score IS NULL OR confidence_score BETWEEN 0 AND 1);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_location ON anomaly_events (latitude, longitude);

CREATE TABLE IF NOT EXISTS vessel_draught_observations (
    id         BIGSERIAL PRIMARY KEY,
    vessel_id  BIGINT REFERENCES vessels (id) ON DELETE CASCADE,
    mmsi       VARCHAR(20) NOT NULL,
    timestamp  TIMESTAMPTZ NOT NULL,
    draught_m  DOUBLE PRECISION NOT NULL CHECK (draught_m >= 0 AND draught_m <= 30),
    source     VARCHAR(50) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_draught_obs_mmsi_time
    ON vessel_draught_observations (mmsi, timestamp DESC);

ALTER TABLE vessels ADD COLUMN IF NOT EXISTS last_course DOUBLE PRECISION;
ALTER TABLE vessels ADD COLUMN IF NOT EXISTS last_heading DOUBLE PRECISION;
ALTER TABLE vessels ADD COLUMN IF NOT EXISTS last_draught DOUBLE PRECISION;
ALTER TABLE vessels ADD COLUMN IF NOT EXISTS last_draught_at TIMESTAMPTZ;
