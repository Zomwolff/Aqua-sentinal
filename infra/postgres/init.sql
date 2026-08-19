CREATE EXTENSION IF NOT EXISTS postgis;

-- ============================================================
-- Vessels & AIS position feeds
-- ============================================================

CREATE TABLE IF NOT EXISTS vessels (
    mmsi          BIGINT PRIMARY KEY,
    vessel_name   TEXT,
    vessel_type   TEXT,
    first_seen    TIMESTAMPTZ,
    last_seen     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_vessels_first_seen ON vessels (first_seen);
CREATE INDEX IF NOT EXISTS idx_vessels_last_seen ON vessels (last_seen);

CREATE TABLE IF NOT EXISTS vessel_positions (
    id           BIGSERIAL PRIMARY KEY,
    mmsi         BIGINT NOT NULL REFERENCES vessels (mmsi),
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    speed_knots  DOUBLE PRECISION,
    course       DOUBLE PRECISION,
    heading      DOUBLE PRECISION,
    timestamp    TIMESTAMPTZ NOT NULL,
    geom         geography(Point, 4326)
);

CREATE INDEX IF NOT EXISTS idx_vessel_positions_geom ON vessel_positions USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_vessel_positions_mmsi ON vessel_positions (mmsi);
CREATE INDEX IF NOT EXISTS idx_vessel_positions_timestamp ON vessel_positions (timestamp);

-- ============================================================
-- Behavioral analytics
-- ============================================================

CREATE TABLE IF NOT EXISTS vessel_features (
    id                   BIGSERIAL PRIMARY KEY,
    mmsi                 BIGINT NOT NULL REFERENCES vessels (mmsi),
    window_start         TIMESTAMPTZ NOT NULL,
    window_end           TIMESTAMPTZ NOT NULL,
    avg_speed            DOUBLE PRECISION,
    speed_variance       DOUBLE PRECISION,
    course_variance      DOUBLE PRECISION,
    loitering_score      DOUBLE PRECISION,
    distance_traveled_km DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_vessel_features_mmsi ON vessel_features (mmsi);
CREATE INDEX IF NOT EXISTS idx_vessel_features_window_start ON vessel_features (window_start);
CREATE INDEX IF NOT EXISTS idx_vessel_features_window_end ON vessel_features (window_end);

-- ============================================================
-- Anomaly & dark vessel events
-- ============================================================

CREATE TABLE IF NOT EXISTS anomaly_events (
    id           BIGSERIAL PRIMARY KEY,
    mmsi         BIGINT NOT NULL REFERENCES vessels (mmsi),
    window_start TIMESTAMPTZ NOT NULL,
    anomaly_type TEXT,
    severity     TEXT,
    evidence     JSONB
);

CREATE INDEX IF NOT EXISTS idx_anomaly_events_mmsi ON anomaly_events (mmsi);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_window_start ON anomaly_events (window_start);

CREATE TABLE IF NOT EXISTS dark_vessel_events (
    id           BIGSERIAL PRIMARY KEY,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    timestamp    TIMESTAMPTZ NOT NULL,
    image_source TEXT,
    sensor       TEXT,
    confidence   DOUBLE PRECISION,
    geom         geography(Point, 4326)
);

CREATE INDEX IF NOT EXISTS idx_dark_vessel_events_geom ON dark_vessel_events USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_dark_vessel_events_timestamp ON dark_vessel_events (timestamp);

-- ============================================================
-- AIS trust / spoofing
-- ============================================================

CREATE TABLE IF NOT EXISTS ais_trust_scores (
    id                       BIGSERIAL PRIMARY KEY,
    mmsi                     BIGINT NOT NULL REFERENCES vessels (mmsi),
    timestamp                TIMESTAMPTZ NOT NULL,
    discrepancy_distance_m   DOUBLE PRECISION,
    trust_score              DOUBLE PRECISION,
    flag                     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ais_trust_scores_mmsi ON ais_trust_scores (mmsi);
CREATE INDEX IF NOT EXISTS idx_ais_trust_scores_timestamp ON ais_trust_scores (timestamp);

-- ============================================================
-- STS (ship-to-ship) transfers
-- ============================================================

CREATE TABLE IF NOT EXISTS sts_events (
    id               BIGSERIAL PRIMARY KEY,
    vessel_a         BIGINT NOT NULL REFERENCES vessels (mmsi),
    vessel_b         BIGINT NOT NULL REFERENCES vessels (mmsi),
    start_time       TIMESTAMPTZ NOT NULL,
    end_time         TIMESTAMPTZ,
    duration_minutes DOUBLE PRECISION,
    avg_distance_m   DOUBLE PRECISION,
    confidence       DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_sts_events_vessel_a ON sts_events (vessel_a);
CREATE INDEX IF NOT EXISTS idx_sts_events_vessel_b ON sts_events (vessel_b);
CREATE INDEX IF NOT EXISTS idx_sts_events_start_time ON sts_events (start_time);
CREATE INDEX IF NOT EXISTS idx_sts_events_end_time ON sts_events (end_time);

-- ============================================================
-- Risk scoring
-- ============================================================

CREATE TABLE IF NOT EXISTS vessel_risk_scores (
    mmsi                 BIGINT PRIMARY KEY REFERENCES vessels (mmsi),
    risk_score           DOUBLE PRECISION,
    tier                 TEXT,
    contributing_factors JSONB,
    recommended_action   TEXT,
    updated_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_vessel_risk_scores_updated_at ON vessel_risk_scores (updated_at);

-- ============================================================
-- SAR spill candidates & classification
-- ============================================================

CREATE TABLE IF NOT EXISTS spill_candidates (
    id               BIGSERIAL PRIMARY KEY,
    tile_id          TEXT,
    timestamp        TIMESTAMPTZ NOT NULL,
    polygon          geography(Polygon, 4326),
    area_km2         DOUBLE PRECISION,
    texture_features JSONB,
    mean_backscatter DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_spill_candidates_polygon ON spill_candidates USING GIST (polygon);
CREATE INDEX IF NOT EXISTS idx_spill_candidates_timestamp ON spill_candidates (timestamp);

CREATE TABLE IF NOT EXISTS classified_spills (
    candidate_id           BIGINT PRIMARY KEY REFERENCES spill_candidates (id),
    oil_prob               DOUBLE PRECISION,
    biogenic_prob          DOUBLE PRECISION,
    low_wind_prob          DOUBLE PRECISION,
    other_prob             DOUBLE PRECISION,
    final_class            TEXT,
    classifier_confidence  DOUBLE PRECISION
);

-- ============================================================
-- Incident fusion, attribution, drift, severity, response
-- ============================================================

CREATE TABLE IF NOT EXISTS fused_incidents (
    id                 BIGSERIAL PRIMARY KEY,
    spill_polygon      geography(Polygon, 4326),
    timestamp          TIMESTAMPTZ NOT NULL,
    evidence_vector    JSONB,
    overall_confidence DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_fused_incidents_spill_polygon ON fused_incidents USING GIST (spill_polygon);
CREATE INDEX IF NOT EXISTS idx_fused_incidents_timestamp ON fused_incidents (timestamp);

CREATE TABLE IF NOT EXISTS attribution_results (
    id                            BIGSERIAL PRIMARY KEY,
    incident_id                   BIGINT NOT NULL REFERENCES fused_incidents (id),
    mmsi                          BIGINT REFERENCES vessels (mmsi),
    ais_behavior_score            DOUBLE PRECISION,
    spatial_match                 DOUBLE PRECISION,
    temporal_match                DOUBLE PRECISION,
    drift_compatibility           DOUBLE PRECISION,
    vessel_type_score             DOUBLE PRECISION,
    ais_reliability               DOUBLE PRECISION,
    final_attribution_probability DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_attribution_results_incident_id ON attribution_results (incident_id);
CREATE INDEX IF NOT EXISTS idx_attribution_results_mmsi ON attribution_results (mmsi);

CREATE TABLE IF NOT EXISTS drift_forecasts (
    id                   BIGSERIAL PRIMARY KEY,
    incident_id          BIGINT NOT NULL REFERENCES fused_incidents (id),
    horizon              INTERVAL,
    predicted_polygon    geography(Polygon, 4326),
    centroid_distance_km DOUBLE PRECISION,
    confidence           DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_drift_forecasts_incident_id ON drift_forecasts (incident_id);
CREATE INDEX IF NOT EXISTS idx_drift_forecasts_predicted_polygon ON drift_forecasts USING GIST (predicted_polygon);

CREATE TABLE IF NOT EXISTS severity_assessments (
    incident_id              BIGINT PRIMARY KEY REFERENCES fused_incidents (id),
    severity                 TEXT,
    estimated_area_km2       DOUBLE PRECISION,
    estimated_volume_low     DOUBLE PRECISION,
    estimated_volume_high    DOUBLE PRECISION,
    growth_rate_pct_per_hr   DOUBLE PRECISION,
    coastline_distance_km    DOUBLE PRECISION,
    ecological_exposure      JSONB
);

CREATE TABLE IF NOT EXISTS response_recommendations (
    id              BIGSERIAL PRIMARY KEY,
    incident_id     BIGINT NOT NULL REFERENCES fused_incidents (id),
    priority_actions JSONB,
    generated_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_response_recommendations_incident_id ON response_recommendations (incident_id);
CREATE INDEX IF NOT EXISTS idx_response_recommendations_generated_at ON response_recommendations (generated_at);

-- ============================================================
-- Reference layers (coastlines, protected areas, env grids)
-- ============================================================

CREATE TABLE IF NOT EXISTS reference_layers (
    id         BIGSERIAL PRIMARY KEY,
    layer_type TEXT,
    name       TEXT,
    geom       geography(Geometry, 4326)
);

CREATE INDEX IF NOT EXISTS idx_reference_layers_geom ON reference_layers USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_reference_layers_layer_type ON reference_layers (layer_type);
