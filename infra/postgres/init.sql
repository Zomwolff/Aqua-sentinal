CREATE EXTENSION IF NOT EXISTS postgis;

-- ============================================================
-- Aqua-Sentinel database schema (final 9-table contract)
-- ============================================================

-- ------------------------------------------------------------
-- Table 1: vessels
-- Static registry of physical vessels.
-- mmsi is the external AIS identity; id is the internal FK target.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vessels (
    id            BIGSERIAL PRIMARY KEY,
    imo_number    VARCHAR(20) UNIQUE,
    mmsi          VARCHAR(20) UNIQUE NOT NULL,
    name          VARCHAR(255),
    vessel_type   TEXT,
    flag          VARCHAR(100),
    length_m      NUMERIC,
    width_m       NUMERIC,
    gross_tonnage NUMERIC,
    operator      VARCHAR(255),
    created_at    TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ
);

-- ------------------------------------------------------------
-- Table 2: vessel_positions
-- High-volume AIS position history. One row = one AIS report.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vessel_positions (
    id          BIGSERIAL PRIMARY KEY,
    vessel_id   BIGINT NOT NULL REFERENCES vessels (id),
    timestamp   TIMESTAMPTZ,
    latitude    DOUBLE PRECISION,
    longitude   DOUBLE PRECISION,
    geom        GEOMETRY(Point, 4326),
    speed_knots NUMERIC,
    course_deg  NUMERIC,
    heading_deg NUMERIC,
    nav_status  VARCHAR,
    source      VARCHAR
);

-- ------------------------------------------------------------
-- Table 3: spill_incidents
-- Detected oil-spill incidents. id is a UUID shared across services.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS spill_incidents (
    id              UUID PRIMARY KEY,
    detected_at     TIMESTAMPTZ,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    geom            GEOMETRY(Polygon, 4326),
    centroid        GEOMETRY(Point, 4326) NOT NULL,
    area_km2        NUMERIC,
    confidence      NUMERIC,
    source          TEXT,
    source_image_id VARCHAR,
    status          TEXT
);

-- ------------------------------------------------------------
-- Table 4: attribution_results
-- Candidate source vessels for a spill. One row per candidate.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attribution_results (
    id                BIGSERIAL PRIMARY KEY,
    spill_id          UUID NOT NULL REFERENCES spill_incidents (id),
    vessel_id         BIGINT NOT NULL REFERENCES vessels (id),
    distance_score    NUMERIC,
    trajectory_score  NUMERIC,
    wind_score        NUMERIC,
    time_score        NUMERIC,
    behavior_score    NUMERIC,
    final_score       NUMERIC NOT NULL,
    model_version     VARCHAR,
    computed_at       TIMESTAMPTZ
);

-- ------------------------------------------------------------
-- Table 5: forecasts
-- Predicted future spill movement. One row per forecast horizon.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS forecasts (
    id             BIGSERIAL PRIMARY KEY,
    spill_id       UUID NOT NULL REFERENCES spill_incidents (id),
    forecast_time  TIMESTAMPTZ,
    generated_at   TIMESTAMPTZ,
    horizon_hours  NUMERIC,
    geom           GEOMETRY(Polygon, 4326) NOT NULL,
    model_version  VARCHAR,
    confidence     NUMERIC
);

-- ------------------------------------------------------------
-- Table 6: severity
-- Risk/impact assessment for a spill.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS severity (
    id                   BIGSERIAL PRIMARY KEY,
    spill_id             UUID NOT NULL REFERENCES spill_incidents (id),
    severity_level       TEXT,
    score                NUMERIC NOT NULL,
    environmental_risk   NUMERIC,
    population_risk      NUMERIC,
    economic_risk        NUMERIC,
    protected_area_risk  NUMERIC,
    computed_at          TIMESTAMPTZ
);

-- ------------------------------------------------------------
-- Table 7: response_recommendations
-- Recommended actions for authorities.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS response_recommendations (
    id               BIGSERIAL PRIMARY KEY,
    spill_id         UUID NOT NULL REFERENCES spill_incidents (id),
    recommendation   TEXT,
    priority         TEXT,
    status           TEXT,
    generated_at     TIMESTAMPTZ,
    acknowledged_at  TIMESTAMPTZ,
    acknowledged_by  VARCHAR
);

-- ------------------------------------------------------------
-- Table 8: protected_areas
-- Geographic contextual areas (MPAs, mangroves, ports, zones).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS protected_areas (
    id         BIGSERIAL PRIMARY KEY,
    name       VARCHAR,
    area_type  VARCHAR,
    geom       GEOMETRY(MultiPolygon, 4326) NOT NULL,
    metadata   JSONB
);

-- ------------------------------------------------------------
-- Table 9: environmental_conditions
-- Wind/current observations at a location + time.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS environmental_conditions (
    id                    BIGSERIAL PRIMARY KEY,
    timestamp             TIMESTAMPTZ,
    latitude              DOUBLE PRECISION,
    longitude             DOUBLE PRECISION,
    geom                  GEOMETRY(Point, 4326),
    wind_speed_kmh        NUMERIC,
    wind_direction_deg    NUMERIC,
    current_speed_ms      NUMERIC,
    current_direction_deg NUMERIC,
    source                VARCHAR
);

-- ============================================================
-- Spatial (GIST) indexes
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_vessel_positions_geom
    ON vessel_positions USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_spill_incidents_geom
    ON spill_incidents USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_spill_incidents_centroid
    ON spill_incidents USING GIST (centroid);

CREATE INDEX IF NOT EXISTS idx_forecasts_geom
    ON forecasts USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_protected_areas_geom
    ON protected_areas USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_environmental_conditions_geom
    ON environmental_conditions USING GIST (geom);

-- ============================================================
-- B-tree indexes on FK and time-range access columns
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_vessel_positions_vessel_id
    ON vessel_positions (vessel_id);

CREATE INDEX IF NOT EXISTS idx_vessel_positions_timestamp
    ON vessel_positions (timestamp);

CREATE INDEX IF NOT EXISTS idx_attribution_results_spill_id
    ON attribution_results (spill_id);

CREATE INDEX IF NOT EXISTS idx_attribution_results_vessel_id
    ON attribution_results (vessel_id);

CREATE INDEX IF NOT EXISTS idx_forecasts_spill_id
    ON forecasts (spill_id);

CREATE INDEX IF NOT EXISTS idx_severity_spill_id
    ON severity (spill_id);

CREATE INDEX IF NOT EXISTS idx_response_recommendations_spill_id
    ON response_recommendations (spill_id);

CREATE INDEX IF NOT EXISTS idx_environmental_conditions_timestamp
    ON environmental_conditions (timestamp);
