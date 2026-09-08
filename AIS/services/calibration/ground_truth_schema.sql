/**
 * Ground Truth Schema for AIS Scoring Calibration
 * 
 * Stores labeled incident data used to calibrate anomaly detection thresholds,
 * trust scores, and risk scoring models.
 */

-- Create enum for incident types
CREATE TYPE IF NOT EXISTS ground_truth_incident_type AS ENUM (
    'normal_operation',
    'sudden_stop',
    'erratic_course',
    'speed_anomaly',
    'loitering_anomaly',
    'cog_heading_divergence',
    'draught_drop',
    'route_deviation',
    'ais_gap',
    'dark_vessel',
    'sts_suspicious',
    'sts_illegal',
    'spoofing_confirmed',
    'spoofing_suspicious',
    'other'
);

-- Create enum for severity levels
CREATE TYPE IF NOT EXISTS incident_severity AS ENUM (
    'low',
    'medium',
    'high',
    'critical'
);

-- Create enum for incident status
CREATE TYPE IF NOT EXISTS incident_status AS ENUM (
    'observed',      -- Anomaly detected but unconfirmed
    'confirmed',     -- Confirmed incident (through investigation/satellite/etc)
    'false_positive' -- Confirmed as not a real incident
);

-- Ground truth incidents table
CREATE TABLE IF NOT EXISTS ground_truth_incidents (
    id                  BIGSERIAL PRIMARY KEY,
    mmsi                VARCHAR(20) NOT NULL,
    vessel_id           BIGINT REFERENCES vessels(id) ON DELETE SET NULL,
    vessel_name         VARCHAR(255),
    vessel_type         VARCHAR(50),  -- cargo, tanker, fishing, etc
    
    -- Incident details
    incident_type       ground_truth_incident_type NOT NULL,
    severity            incident_severity,
    status              incident_status NOT NULL DEFAULT 'observed',
    confidence_manual   DOUBLE PRECISION,  -- 0-1, human assessor confidence
    
    -- Detection details
    detected_at         TIMESTAMPTZ NOT NULL,
    detected_method     VARCHAR(100),  -- "anomaly_rule", "sar_correlation", "analyst_report", etc
    
    -- Geographic context
    latitude            DOUBLE PRECISION,
    longitude           DOUBLE PRECISION,
    geom                GEOMETRY(Point, 4326),
    region              VARCHAR(50),  -- "mumbai_offshore", "global", etc
    eez_waters          BOOLEAN,
    port_nearby         BOOLEAN,
    
    -- Temporal context
    season              VARCHAR(20),  -- "winter", "monsoon", "summer"
    weather_conditions  VARCHAR(255),  -- "severe_wind", "high_current", "clear", etc
    
    -- Anomaly details (dynamic features at time of detection)
    anomaly_avg_speed   DOUBLE PRECISION,
    anomaly_course_var  DOUBLE PRECISION,
    anomaly_loitering   DOUBLE PRECISION,
    anomaly_gap_minutes DOUBLE PRECISION,
    
    -- Investigation results
    confirmed_at        TIMESTAMPTZ,
    confirmed_by        VARCHAR(100),  -- analyst name or automated system
    confirmation_notes  TEXT,
    
    -- Metadata
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source              VARCHAR(100),  -- "manual_entry", "sar_analysis", "api_import"
    tags                TEXT[],  -- e.g., ["training_data", "validation_data"]
    
    CONSTRAINT unique_incident_per_mmsi_type_date 
        UNIQUE (mmsi, incident_type, detected_at)
);

CREATE INDEX idx_ground_truth_incidents_mmsi ON ground_truth_incidents (mmsi);
CREATE INDEX idx_ground_truth_incidents_type ON ground_truth_incidents (incident_type);
CREATE INDEX idx_ground_truth_incidents_status ON ground_truth_incidents (status);
CREATE INDEX idx_ground_truth_incidents_detected ON ground_truth_incidents (detected_at);
CREATE INDEX idx_ground_truth_incidents_geom ON ground_truth_incidents USING GIST (geom);
CREATE INDEX idx_ground_truth_incidents_region ON ground_truth_incidents (region);
CREATE INDEX idx_ground_truth_incidents_tags ON ground_truth_incidents USING GIN (tags);

-- Calibration results table (stores outputs of calibration analysis)
CREATE TABLE IF NOT EXISTS calibration_results (
    id                  BIGSERIAL PRIMARY KEY,
    calibration_version VARCHAR(50) NOT NULL UNIQUE,  -- "v2_bayesian_2024q1"
    
    -- Calibration metadata
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    analyst_notes       TEXT,
    ground_truth_count  INTEGER,  -- how many incidents used
    
    -- Threshold results
    threshold_config    JSONB NOT NULL,  -- Full calibration_config.json
    
    -- Validation metrics
    precision           DOUBLE PRECISION,
    recall              DOUBLE PRECISION,
    f1_score            DOUBLE PRECISION,
    roc_auc             DOUBLE PRECISION,
    
    -- Active status
    is_active           BOOLEAN DEFAULT FALSE,
    activated_at        TIMESTAMPTZ,
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_calibration_results_version ON calibration_results (calibration_version);
CREATE INDEX idx_calibration_results_active ON calibration_results (is_active);

-- Model coefficients table (for storing learned Bayesian parameters)
CREATE TABLE IF NOT EXISTS model_coefficients (
    id                  BIGSERIAL PRIMARY KEY,
    model_type          VARCHAR(100) NOT NULL,  -- "trust_score", "dark_vessel_ais_gap", etc
    model_version       VARCHAR(50) NOT NULL,
    
    -- Coefficient details
    feature_name        VARCHAR(100) NOT NULL,
    coefficient         DOUBLE PRECISION,
    intercept           DOUBLE PRECISION,
    confidence_interval NUMRANGE,  -- PostgreSQL range type
    
    -- Model metadata
    training_data_count INTEGER,
    training_date       TIMESTAMPTZ NOT NULL,
    validation_metrics  JSONB,  -- precision, recall, calibration error, etc
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT unique_model_feature 
        UNIQUE (model_type, model_version, feature_name)
);

CREATE INDEX idx_model_coefficients_model ON model_coefficients (model_type, model_version);

-- Calibration validation results (ROC/PR curves, threshold analysis)
CREATE TABLE IF NOT EXISTS calibration_validation (
    id                  BIGSERIAL PRIMARY KEY,
    calibration_version VARCHAR(50) REFERENCES calibration_results(calibration_version),
    
    -- Threshold analysis
    threshold_value     DOUBLE PRECISION,
    true_positives      INTEGER,
    false_positives     INTEGER,
    true_negatives      INTEGER,
    false_negatives     INTEGER,
    
    -- Computed metrics
    sensitivity         DOUBLE PRECISION,  -- TP / (TP + FN)
    specificity         DOUBLE PRECISION,  -- TN / (TN + FP)
    precision           DOUBLE PRECISION,  -- TP / (TP + FP)
    f1                  DOUBLE PRECISION,
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_calibration_validation_version ON calibration_validation (calibration_version);

-- False positive analysis (track sources of false positives)
CREATE TABLE IF NOT EXISTS false_positive_analysis (
    id                  BIGSERIAL PRIMARY KEY,
    incident_id         BIGINT REFERENCES ground_truth_incidents(id) ON DELETE SET NULL,
    
    -- False positive categorization
    fp_category         VARCHAR(100),  -- "port_operation", "weather_event", "legitimate_spoofing", etc
    vessel_type         VARCHAR(50),
    region              VARCHAR(50),
    
    -- Context
    detected_at         TIMESTAMPTZ,
    reason_false        TEXT,  -- why was this flagged incorrectly?
    
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_false_positive_analysis_category ON false_positive_analysis (fp_category);
CREATE INDEX idx_false_positive_analysis_detected ON false_positive_analysis (detected_at);

-- Grant permissions
GRANT SELECT, INSERT, UPDATE ON ground_truth_incidents TO ais_worker;
GRANT SELECT ON calibration_results TO ais_worker;
GRANT SELECT ON model_coefficients TO ais_worker;
GRANT SELECT ON calibration_validation TO ais_worker;

-- Add helpful views
CREATE OR REPLACE VIEW calibration_incident_summary AS
SELECT
    incident_type,
    status,
    COUNT(*) as incident_count,
    COUNT(CASE WHEN status = 'confirmed' THEN 1 END) as confirmed_count,
    COUNT(CASE WHEN status = 'false_positive' THEN 1 END) as false_positive_count,
    ROUND(AVG(confidence_manual)::NUMERIC, 3) as avg_confidence,
    STRING_AGG(DISTINCT region, ', ') as regions
FROM ground_truth_incidents
GROUP BY incident_type, status
ORDER BY incident_type, status;

CREATE OR REPLACE VIEW calibration_false_positive_summary AS
SELECT
    fp_category,
    vessel_type,
    COUNT(*) as fp_count,
    ROUND(100.0 * COUNT(*) / 
        (SELECT COUNT(*) FROM false_positive_analysis)::NUMERIC, 1) as percentage_of_total
FROM false_positive_analysis
GROUP BY fp_category, vessel_type
ORDER BY fp_count DESC;

-- Comments for documentation
COMMENT ON TABLE ground_truth_incidents IS 
    'Labeled incidents for calibration and validation of AIS anomaly detection. 
     Stores confirmed positive examples, confirmed false positives, and unconfirmed detections.';

COMMENT ON TABLE calibration_results IS
    'Stores outputs of calibration analysis runs. Includes threshold configurations,
     validation metrics, and active status for model deployment.';

COMMENT ON TABLE model_coefficients IS
    'Stores learned Bayesian model parameters (coefficients, intercepts, confidence intervals)
     for reproducibility and audit trails.';

COMMENT ON TABLE false_positive_analysis IS
    'Analysis and categorization of false positives to identify systematic issues and
     opportunities for false positive reduction through filtering and weighting.';
