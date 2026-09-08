-- ============================================================
-- Aqua-Sentinel PostgreSQL Schema v2
-- Follows exact entity spec from design document.
-- Extensions
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- ENUMS
-- ============================================================

DO $$ BEGIN
    CREATE TYPE vessel_type_enum AS ENUM (
        'tanker', 'cargo', 'fishing', 'tug', 'passenger',
        'high_speed', 'sailing', 'pleasure', 'pilot',
        'search_and_rescue', 'dredging', 'diving', 'military',
        'other', 'unknown'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE spill_source_enum AS ENUM (
        'sar_satellite', 'manual_report', 'aerial_survey',
        'vessel_report', 'coastal_observation', 'drone'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE spill_status_enum AS ENUM (
        'detected', 'confirmed', 'monitoring', 'contained', 'closed', 'false_positive'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE severity_level_enum AS ENUM ('LOW', 'MODERATE', 'HIGH', 'CRITICAL');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE recommendation_priority_enum AS ENUM ('LOW', 'MEDIUM', 'HIGH', 'URGENT');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE recommendation_status_enum AS ENUM (
        'pending', 'acknowledged', 'in_progress', 'completed', 'rejected'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE risk_tier_enum AS ENUM ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE spill_candidate_status_enum AS ENUM (
        'raw', 'likely_ship_shadow', 'likely_calm_water', 'possible_slick',
        'possible_oil_spill', 'low_confidence'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================
-- 1. vessels — static registry, one row per physical ship
-- ============================================================

CREATE TABLE IF NOT EXISTS vessels (
    id              BIGSERIAL PRIMARY KEY,
    imo_number      VARCHAR(20) UNIQUE,                   -- IMO registry number (nullable for small vessels)
    mmsi            VARCHAR(20) UNIQUE NOT NULL,          -- AIS broadcast ID — primary join key
    name            VARCHAR(255),                         -- vessel name
    vessel_type     vessel_type_enum NOT NULL DEFAULT 'unknown',
    flag            VARCHAR(100),                         -- flag state / country
    length_m        NUMERIC(8,2),                         -- overall length in metres
    width_m         NUMERIC(8,2),                         -- beam in metres
    gross_tonnage   NUMERIC(12,2),                        -- gross tonnage (GT)
    operator        VARCHAR(255),                         -- owning/operating company
    call_sign       VARCHAR(20),                          -- radio callsign
    draught         NUMERIC(6,2),                         -- maximum static draught metres
    destination     VARCHAR(255),                         -- last reported destination
    eta             TIMESTAMPTZ,                          -- last reported ETA
    cargo_type      VARCHAR(100),                         -- cargo description
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Convenience columns for fast last-position lookups (denormalised cache)
ALTER TABLE vessels ADD COLUMN IF NOT EXISTS last_lat   DOUBLE PRECISION;
ALTER TABLE vessels ADD COLUMN IF NOT EXISTS last_lon   DOUBLE PRECISION;
ALTER TABLE vessels ADD COLUMN IF NOT EXISTS last_seen  TIMESTAMPTZ;
ALTER TABLE vessels ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_vessels_mmsi        ON vessels (mmsi);
CREATE INDEX IF NOT EXISTS idx_vessels_imo         ON vessels (imo_number);
CREATE INDEX IF NOT EXISTS idx_vessels_last_seen   ON vessels (last_seen);
CREATE INDEX IF NOT EXISTS idx_vessels_vessel_type ON vessels (vessel_type);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION vessels_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END $$;

DROP TRIGGER IF EXISTS trg_vessels_updated_at ON vessels;
CREATE TRIGGER trg_vessels_updated_at
    BEFORE UPDATE ON vessels
    FOR EACH ROW EXECUTE FUNCTION vessels_set_updated_at();

-- ============================================================
-- 2. vessel_positions — high-volume time series, one row per AIS ping
-- ============================================================

CREATE TABLE IF NOT EXISTS vessel_positions (
    id           BIGSERIAL PRIMARY KEY,
    vessel_id    BIGINT NOT NULL REFERENCES vessels (id) ON DELETE CASCADE,
    timestamp    TIMESTAMPTZ NOT NULL,
    latitude     DOUBLE PRECISION NOT NULL,
    longitude    DOUBLE PRECISION NOT NULL,
    geom         GEOMETRY(Point, 4326),                  -- auto-derived by trigger
    speed_knots  NUMERIC(7,3),
    course_deg   NUMERIC(6,2),
    heading_deg  NUMERIC(6,2),
    nav_status   VARCHAR(50),                            -- e.g. "under way", "at anchor"
    source       VARCHAR(50) NOT NULL DEFAULT 'ais',
    quality_flag VARCHAR(20) NOT NULL DEFAULT 'raw'      -- raw | interpolated | corrected
);

-- Auto-populate geom from lat/lon
CREATE OR REPLACE FUNCTION vessel_positions_set_geom()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.geom = ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_vessel_positions_geom ON vessel_positions;
CREATE TRIGGER trg_vessel_positions_geom
    BEFORE INSERT OR UPDATE ON vessel_positions
    FOR EACH ROW EXECUTE FUNCTION vessel_positions_set_geom();

CREATE INDEX IF NOT EXISTS idx_vessel_positions_vessel_id  ON vessel_positions (vessel_id);
CREATE INDEX IF NOT EXISTS idx_vessel_positions_timestamp  ON vessel_positions (timestamp);
CREATE INDEX IF NOT EXISTS idx_vessel_positions_geom       ON vessel_positions USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_vessel_positions_ts_vid     ON vessel_positions (vessel_id, timestamp DESC);

-- ============================================================
-- 3. spill_incidents — one row per detected spill
-- ============================================================

CREATE TABLE IF NOT EXISTS spill_incidents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    latitude        DOUBLE PRECISION NOT NULL,            -- quick-access centroid lat
    longitude       DOUBLE PRECISION NOT NULL,            -- quick-access centroid lon
    geom            GEOMETRY(Polygon, 4326),              -- actual spill boundary (nullable)
    centroid        GEOMETRY(Point, 4326) NOT NULL,       -- always populated, used for fast distance queries
    area_km2        NUMERIC(12,4),
    confidence      NUMERIC(5,4) CHECK (confidence BETWEEN 0 AND 1),
    source          spill_source_enum NOT NULL DEFAULT 'sar_satellite',
    source_image_id VARCHAR(255),                         -- e.g. SAR scene filename
    status          spill_status_enum NOT NULL DEFAULT 'detected'
);

-- Auto-populate centroid from lat/lon when geom is not provided
CREATE OR REPLACE FUNCTION spill_incidents_set_centroid()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.centroid IS NULL THEN
        NEW.centroid = ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_spill_incidents_centroid ON spill_incidents;
CREATE TRIGGER trg_spill_incidents_centroid
    BEFORE INSERT OR UPDATE ON spill_incidents
    FOR EACH ROW EXECUTE FUNCTION spill_incidents_set_centroid();

CREATE INDEX IF NOT EXISTS idx_spill_incidents_centroid    ON spill_incidents USING GIST (centroid);
CREATE INDEX IF NOT EXISTS idx_spill_incidents_geom        ON spill_incidents USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_spill_incidents_detected_at ON spill_incidents (detected_at);
CREATE INDEX IF NOT EXISTS idx_spill_incidents_status      ON spill_incidents (status);

-- ============================================================
-- 3b. spill_candidates — raw SAR detections before classification
-- ============================================================

CREATE TABLE IF NOT EXISTS spill_candidates (
    candidate_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scene_id             VARCHAR(255) NOT NULL,               -- SAR scene id (e.g. COPERNICUS/S1_GRD/...)
    acquisition_time     TIMESTAMPTZ NOT NULL,                -- SAR acquisition time
    geom                 GEOMETRY(Polygon, 4326) NOT NULL,    -- candidate spill boundary (EPSG:4326, lon/lat)
    area_m2              NUMERIC(14,2) NOT NULL CHECK (area_m2 >= 0),  -- geography-cast area in m²
    pixel_count          INTEGER NOT NULL CHECK (pixel_count > 0),
    status               spill_candidate_status_enum NOT NULL DEFAULT 'raw',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confidence           DOUBLE PRECISION CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),  -- Step 5 heuristic confidence
    classification_label spill_candidate_status_enum,        -- Step 4/5 classification label
    texture_features     JSONB,                              -- Step 5 GLCM texture features
    is_synthetic         BOOLEAN NOT NULL DEFAULT FALSE,     -- provenance: synthetic demo injection
    -- B2 Look-Alike Classifier features (nullable; see migrations/002_b2_features.sql)
    mean_backscatter     DOUBLE PRECISION,                   -- raw SAR dB mean in candidate
    std_backscatter      DOUBLE PRECISION,                   -- raw SAR dB std in candidate
    glcm_contrast        DOUBLE PRECISION,                   -- authoritative B2 source: geo_postprocess.py
    glcm_homogeneity     DOUBLE PRECISION,
    glcm_energy          DOUBLE PRECISION,                   -- true energy = sqrt(ASM)
    glcm_correlation     DOUBLE PRECISION,
    perimeter_m          DOUBLE PRECISION CHECK (perimeter_m IS NULL OR perimeter_m > 0),
    elongation           DOUBLE PRECISION CHECK (elongation IS NULL OR elongation >= 0),
    boundary_irregularity DOUBLE PRECISION CHECK (boundary_irregularity IS NULL OR boundary_irregularity > 0),
    edge_sharpness       DOUBLE PRECISION,                   -- outside-minus-inside boundary SAR (dB)
    wind_speed_kmh       DOUBLE PRECISION,                   -- 10-m wind, NULL when unmatched
    distance_to_nearest_vessel_km DOUBLE PRECISION CHECK (distance_to_nearest_vessel_km IS NULL OR distance_to_nearest_vessel_km >= 0),
    persistence_count    INTEGER CHECK (persistence_count IS NULL OR persistence_count >= 1)
);

CREATE INDEX IF NOT EXISTS idx_spill_candidates_geom       ON spill_candidates USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_spill_candidates_scene_id   ON spill_candidates (scene_id);
CREATE INDEX IF NOT EXISTS idx_spill_candidates_acquisition ON spill_candidates (acquisition_time);
CREATE INDEX IF NOT EXISTS idx_spill_candidates_status     ON spill_candidates (status);

-- ============================================================
-- 4. attribution_results — candidate vessels per spill, scored
-- ============================================================

CREATE TABLE IF NOT EXISTS attribution_results (
    id                 BIGSERIAL PRIMARY KEY,
    spill_id           UUID NOT NULL REFERENCES spill_incidents (id) ON DELETE CASCADE,
    vessel_id          BIGINT NOT NULL REFERENCES vessels (id) ON DELETE CASCADE,
    distance_score     NUMERIC(5,4) CHECK (distance_score BETWEEN 0 AND 1),     -- proximity to spill centroid
    trajectory_score   NUMERIC(5,4) CHECK (trajectory_score BETWEEN 0 AND 1),   -- AIS track passes through spill
    wind_score         NUMERIC(5,4) CHECK (wind_score BETWEEN 0 AND 1),         -- drift-corrected trajectory match
    time_score         NUMERIC(5,4) CHECK (time_score BETWEEN 0 AND 1),         -- temporal coincidence
    behavior_score     NUMERIC(5,4) CHECK (behavior_score BETWEEN 0 AND 1),     -- anomalous behaviour signals
    final_score        NUMERIC(5,4) NOT NULL CHECK (final_score BETWEEN 0 AND 1),
    model_version      VARCHAR(50) NOT NULL DEFAULT '1.0',
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (spill_id, vessel_id, model_version)
);

CREATE INDEX IF NOT EXISTS idx_attribution_spill_id   ON attribution_results (spill_id);
CREATE INDEX IF NOT EXISTS idx_attribution_vessel_id  ON attribution_results (vessel_id);
CREATE INDEX IF NOT EXISTS idx_attribution_final_score ON attribution_results (final_score DESC);

-- ============================================================
-- 5. forecasts — predicted spill movement over time
-- ============================================================

CREATE TABLE IF NOT EXISTS forecasts (
    id             BIGSERIAL PRIMARY KEY,
    spill_id       UUID NOT NULL REFERENCES spill_incidents (id) ON DELETE CASCADE,
    forecast_time  TIMESTAMPTZ NOT NULL,                -- the future moment this prediction is for
    generated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(), -- when the prediction was made
    horizon_hours  NUMERIC(6,2) NOT NULL,              -- 1, 3, 6, 12, 24 etc.
    geom           GEOMETRY(Polygon, 4326) NOT NULL,   -- predicted spill shape at that horizon
    model_version  VARCHAR(50) NOT NULL DEFAULT '1.0',
    confidence     NUMERIC(5,4) CHECK (confidence BETWEEN 0 AND 1)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_spill_id      ON forecasts (spill_id);
CREATE INDEX IF NOT EXISTS idx_forecasts_forecast_time ON forecasts (forecast_time);
CREATE INDEX IF NOT EXISTS idx_forecasts_geom          ON forecasts USING GIST (geom);

-- ============================================================
-- 6. severity — risk assessment per spill
-- ============================================================

CREATE TABLE IF NOT EXISTS severity (
    id                   BIGSERIAL PRIMARY KEY,
    spill_id             UUID NOT NULL REFERENCES spill_incidents (id) ON DELETE CASCADE,
    severity_level       severity_level_enum NOT NULL,
    score                NUMERIC(5,4) NOT NULL CHECK (score BETWEEN 0 AND 1),
    environmental_risk   NUMERIC(5,4) CHECK (environmental_risk BETWEEN 0 AND 1),
    population_risk      NUMERIC(5,4) CHECK (population_risk BETWEEN 0 AND 1),
    economic_risk        NUMERIC(5,4) CHECK (economic_risk BETWEEN 0 AND 1),
    protected_area_risk  NUMERIC(5,4) CHECK (protected_area_risk BETWEEN 0 AND 1),
    computed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_severity_spill_id    ON severity (spill_id);
CREATE INDEX IF NOT EXISTS idx_severity_computed_at ON severity (computed_at);
CREATE INDEX IF NOT EXISTS idx_severity_level       ON severity (severity_level);

-- ============================================================
-- 7. response_recommendations — suggested actions for authorities
-- ============================================================

CREATE TABLE IF NOT EXISTS response_recommendations (
    id               BIGSERIAL PRIMARY KEY,
    spill_id         UUID NOT NULL REFERENCES spill_incidents (id) ON DELETE CASCADE,
    recommendation   TEXT NOT NULL,                    -- e.g. "Deploy containment boom at 18.9°N, 72.8°E"
    priority         recommendation_priority_enum NOT NULL DEFAULT 'MEDIUM',
    status           recommendation_status_enum NOT NULL DEFAULT 'pending',
    generated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged_at  TIMESTAMPTZ,
    acknowledged_by  VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS idx_response_reco_spill_id     ON response_recommendations (spill_id);
CREATE INDEX IF NOT EXISTS idx_response_reco_status       ON response_recommendations (status);
CREATE INDEX IF NOT EXISTS idx_response_reco_generated_at ON response_recommendations (generated_at);

-- ============================================================
-- 8. protected_areas — marine protected areas, ports, fishing zones
-- ============================================================

CREATE TABLE IF NOT EXISTS protected_areas (
    id         BIGSERIAL PRIMARY KEY,
    name       VARCHAR(255) NOT NULL,
    area_type  VARCHAR(100) NOT NULL,    -- 'marine_protected_area' | 'mangrove' | 'port' | 'fishing_zone' | 'eez'
    geom       GEOMETRY(MultiPolygon, 4326) NOT NULL,
    metadata   JSONB                     -- flexible extra attributes (e.g. from GeoJSON source)
);

CREATE INDEX IF NOT EXISTS idx_protected_areas_geom      ON protected_areas USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_protected_areas_area_type ON protected_areas (area_type);

-- ============================================================
-- 9. environmental_conditions — wind / current samples
-- ============================================================

CREATE TABLE IF NOT EXISTS environmental_conditions (
    id                     BIGSERIAL PRIMARY KEY,
    timestamp              TIMESTAMPTZ NOT NULL,
    latitude               DOUBLE PRECISION NOT NULL,
    longitude              DOUBLE PRECISION NOT NULL,
    geom                   GEOMETRY(Point, 4326),
    wind_speed_kmh         NUMERIC(7,3),
    wind_direction_deg     NUMERIC(6,2),
    current_speed_ms       NUMERIC(7,4),
    current_direction_deg  NUMERIC(6,2),
    source                 VARCHAR(100) NOT NULL DEFAULT 'era5'  -- era5 | gfs | cmems | in_situ
);

-- Auto-populate geom
CREATE OR REPLACE FUNCTION env_conditions_set_geom()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.geom = ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_env_conditions_geom ON environmental_conditions;
CREATE TRIGGER trg_env_conditions_geom
    BEFORE INSERT OR UPDATE ON environmental_conditions
    FOR EACH ROW EXECUTE FUNCTION env_conditions_set_geom();

CREATE INDEX IF NOT EXISTS idx_env_conditions_geom      ON environmental_conditions USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_env_conditions_timestamp ON environmental_conditions (timestamp);

-- ============================================================
-- AIS PIPELINE TABLES (supporting the detection pipeline)
-- ============================================================

-- Behavioral feature windows (output of ais-analytics)
CREATE TABLE IF NOT EXISTS vessel_features (
    id                    BIGSERIAL PRIMARY KEY,
    vessel_id             BIGINT NOT NULL REFERENCES vessels (id) ON DELETE CASCADE,
    mmsi                  VARCHAR(20) NOT NULL,           -- denormalised for fast lookup
    window_start          TIMESTAMPTZ NOT NULL,
    window_end            TIMESTAMPTZ NOT NULL,
    ping_count            INTEGER NOT NULL DEFAULT 0,
    avg_speed             DOUBLE PRECISION,
    speed_variance        DOUBLE PRECISION,
    max_speed             DOUBLE PRECISION,
    course_variance       DOUBLE PRECISION,               -- circular variance (not naive)
    heading_change_rate   DOUBLE PRECISION,               -- degrees per minute
    loitering_score       DOUBLE PRECISION,               -- 0-1, port-proximity discounted
    distance_traveled_km  DOUBLE PRECISION,
    proximity_events      JSONB,                          -- [{mmsi, distance_m, timestamp}]
    quality               TEXT NOT NULL DEFAULT 'ok'     -- ok | low | insufficient
);

CREATE INDEX IF NOT EXISTS idx_vessel_features_vessel_id    ON vessel_features (vessel_id);
CREATE INDEX IF NOT EXISTS idx_vessel_features_mmsi         ON vessel_features (mmsi);
CREATE INDEX IF NOT EXISTS idx_vessel_features_window_end   ON vessel_features (window_end);

-- Stage 1 motion features.  These are intentionally separate from the raw
-- pings so every alert can be reproduced from a bounded analysis window.
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS max_speed_delta_kn DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS max_speed_drop_kn DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS mean_cog_heading_divergence_deg DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS max_cog_heading_divergence_deg DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS repeated_cog_heading_divergences INTEGER NOT NULL DEFAULT 0;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS max_rate_of_turn_deg_min DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS turn_reversal_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS draught_m DOUBLE PRECISION;
ALTER TABLE vessel_features ADD COLUMN IF NOT EXISTS draught_change_m DOUBLE PRECISION;

-- Anomaly events (output of anomaly-detection)
CREATE TABLE IF NOT EXISTS anomaly_events (
    id           BIGSERIAL PRIMARY KEY,
    vessel_id    BIGINT REFERENCES vessels (id) ON DELETE SET NULL,
    mmsi         VARCHAR(20) NOT NULL,                   -- denormalised for fast lookup
    window_start TIMESTAMPTZ NOT NULL,
    anomaly_type TEXT NOT NULL,
    severity     TEXT NOT NULL,                          -- LOW | MEDIUM | HIGH
    evidence     JSONB,
    source       TEXT NOT NULL DEFAULT 'rules'           -- rules | statistical | population_prior
);

CREATE INDEX IF NOT EXISTS idx_anomaly_events_vessel_id    ON anomaly_events (vessel_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_mmsi         ON anomaly_events (mmsi);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_window_start ON anomaly_events (window_start);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_severity     ON anomaly_events (severity);

-- Required Stage 1 AOI output: the position and a calibrated 0–1 confidence
-- are saved with the alert, not inferred later from a moving vessel record.
ALTER TABLE anomaly_events ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION;
ALTER TABLE anomaly_events ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;
ALTER TABLE anomaly_events ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION
    CHECK (confidence_score IS NULL OR confidence_score BETWEEN 0 AND 1);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_location ON anomaly_events (latitude, longitude);

-- Draught is an AIS static/dynamic field when a source supplies it.  Keeping
-- observations prevents a current value from being mistaken for a change.
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

-- AIS trust / spoofing scores (output of ais-spoof-detection)
CREATE TABLE IF NOT EXISTS ais_trust_scores (
    id                     BIGSERIAL PRIMARY KEY,
    vessel_id              BIGINT REFERENCES vessels (id) ON DELETE SET NULL,
    mmsi                   VARCHAR(20) NOT NULL,
    timestamp              TIMESTAMPTZ NOT NULL,
    discrepancy_distance_m DOUBLE PRECISION,
    instant_trust_score    DOUBLE PRECISION,
    rolling_trust_score    DOUBLE PRECISION,
    speed_jump_flag        BOOLEAN NOT NULL DEFAULT FALSE,
    identity_change_flag   BOOLEAN NOT NULL DEFAULT FALSE,
    mmsi_validity_flag     BOOLEAN NOT NULL DEFAULT TRUE,
    flag                   TEXT                          -- null | spoofing_suspected
);

CREATE INDEX IF NOT EXISTS idx_ais_trust_vessel_id  ON ais_trust_scores (vessel_id);
CREATE INDEX IF NOT EXISTS idx_ais_trust_mmsi       ON ais_trust_scores (mmsi);
CREATE INDEX IF NOT EXISTS idx_ais_trust_timestamp  ON ais_trust_scores (timestamp);

-- STS (ship-to-ship) transfer encounters (output of sts-detection)
CREATE TABLE IF NOT EXISTS sts_events (
    id                       BIGSERIAL PRIMARY KEY,
    vessel_a_id              BIGINT REFERENCES vessels (id) ON DELETE SET NULL,
    vessel_b_id              BIGINT REFERENCES vessels (id) ON DELETE SET NULL,
    vessel_a_mmsi            VARCHAR(20) NOT NULL,
    vessel_b_mmsi            VARCHAR(20) NOT NULL,
    start_time               TIMESTAMPTZ NOT NULL,
    end_time                 TIMESTAMPTZ,
    duration_minutes         DOUBLE PRECISION,
    avg_distance_m           DOUBLE PRECISION,
    min_distance_m           DOUBLE PRECISION,
    avg_combined_speed_knots DOUBLE PRECISION,
    confidence               DOUBLE PRECISION,
    location                 GEOMETRY(Point, 4326)       -- centroid of encounter
);

CREATE INDEX IF NOT EXISTS idx_sts_vessel_a ON sts_events (vessel_a_mmsi);
CREATE INDEX IF NOT EXISTS idx_sts_vessel_b ON sts_events (vessel_b_mmsi);
CREATE INDEX IF NOT EXISTS idx_sts_start    ON sts_events (start_time);
CREATE INDEX IF NOT EXISTS idx_sts_geom     ON sts_events USING GIST (location);

-- Vessel risk scores (output of vessel-risk-engine)
CREATE TABLE IF NOT EXISTS vessel_risk_scores (
    vessel_id            BIGINT PRIMARY KEY REFERENCES vessels (id) ON DELETE CASCADE,
    mmsi                 VARCHAR(20) NOT NULL UNIQUE,
    risk_score           DOUBLE PRECISION NOT NULL DEFAULT 0,
    tier                 risk_tier_enum NOT NULL DEFAULT 'LOW',
    contributing_factors JSONB,
    recommended_action   TEXT,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_risk_scores_tier       ON vessel_risk_scores (tier);
CREATE INDEX IF NOT EXISTS idx_risk_scores_risk_score ON vessel_risk_scores (risk_score DESC);
CREATE INDEX IF NOT EXISTS idx_risk_scores_updated_at ON vessel_risk_scores (updated_at);

-- Dark vessel detections (SAR-detected vessels with no AIS)
CREATE TABLE IF NOT EXISTS dark_vessel_events (
    id             BIGSERIAL PRIMARY KEY,
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    latitude       DOUBLE PRECISION NOT NULL,
    longitude      DOUBLE PRECISION NOT NULL,
    geom           GEOMETRY(Point, 4326),
    image_source   VARCHAR(255),
    sensor         VARCHAR(100),
    confidence     DOUBLE PRECISION,
    length_est_m   DOUBLE PRECISION,                    -- estimated vessel length from SAR
    matched_mmsi   VARCHAR(20),                         -- if subsequently matched to an AIS vessel
    matched_vessel_id BIGINT REFERENCES vessels (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_dark_vessel_geom      ON dark_vessel_events USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_dark_vessel_detected  ON dark_vessel_events (detected_at);

-- Satellite tasking requests (from risk engine for HIGH/CRITICAL vessels)
CREATE TABLE IF NOT EXISTS satellite_tasking_requests (
    id           BIGSERIAL PRIMARY KEY,
    vessel_id    BIGINT REFERENCES vessels (id) ON DELETE SET NULL,
    mmsi         VARCHAR(20) NOT NULL,
    risk_score   DOUBLE PRECISION,
    risk_tier    risk_tier_enum,
    reason       JSONB,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status       TEXT NOT NULL DEFAULT 'pending'        -- pending | processing | acknowledged | fulfilled | failed
);

-- Idempotent column additions for volumes created with older init.sql revisions
-- (CREATE TABLE IF NOT EXISTS alone does not evolve existing tables).
ALTER TABLE satellite_tasking_requests ADD COLUMN IF NOT EXISTS scene_id     VARCHAR(255);
ALTER TABLE satellite_tasking_requests ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_tasking_status_requested ON satellite_tasking_requests (status, requested_at);
CREATE INDEX IF NOT EXISTS idx_dark_vessel_detected_at ON dark_vessel_events (detected_at);

CREATE INDEX IF NOT EXISTS idx_sat_tasking_vessel_id   ON satellite_tasking_requests (vessel_id);
CREATE INDEX IF NOT EXISTS idx_sat_tasking_requested   ON satellite_tasking_requests (requested_at);

-- Ingestion monitoring stats
CREATE TABLE IF NOT EXISTS ingest_stats (
    id                BIGSERIAL PRIMARY KEY,
    source_type       TEXT NOT NULL,                    -- ais | sar | env
    records_accepted  INTEGER NOT NULL DEFAULT 0,
    records_rejected  INTEGER NOT NULL DEFAULT 0,
    rejection_reasons JSONB,
    window_start      TIMESTAMPTZ NOT NULL,
    window_end        TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingest_stats_window ON ingest_stats (window_start);
CREATE INDEX IF NOT EXISTS idx_ingest_stats_source ON ingest_stats (source_type);

-- Welford online stats (anomaly detection state)
CREATE TABLE IF NOT EXISTS vessel_running_stats (
    vessel_id         BIGINT PRIMARY KEY REFERENCES vessels (id) ON DELETE CASCADE,
    mmsi              VARCHAR(20) NOT NULL UNIQUE,
    window_count      INTEGER NOT NULL DEFAULT 0,
    mean_speed        DOUBLE PRECISION,
    m2_speed          DOUBLE PRECISION,
    mean_course_var   DOUBLE PRECISION,
    m2_course_var     DOUBLE PRECISION,
    mean_distance     DOUBLE PRECISION,
    m2_distance       DOUBLE PRECISION,
    updated_at        TIMESTAMPTZ
);

-- Reference layers (ports, coastlines, EEZ, protected zones)
CREATE TABLE IF NOT EXISTS reference_layers (
    id         BIGSERIAL PRIMARY KEY,
    layer_type TEXT NOT NULL,                           -- coastline | port | protected_zone | anchorage | eez
    name       TEXT,
    geom       GEOMETRY(Geometry, 4326)
);

CREATE INDEX IF NOT EXISTS idx_reference_layers_geom      ON reference_layers USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_reference_layers_type      ON reference_layers (layer_type);
