-- Historical incident context layer. This is deliberately separate from
-- spill_incidents, which is the live detection/attribution table.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS sources (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL CHECK (source_type IN (
        'official_government', 'industry_ngo', 'academic_dataset',
        'news_media', 'satellite_detection', 'crowd_wiki', 'synthetic_demo'
    )),
    url TEXT,
    license TEXT,
    reliability_tier SMALLINT NOT NULL CHECK (reliability_tier BETWEEN 1 AND 5),
    last_verified_at DATE,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS incidents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title TEXT NOT NULL,
    description TEXT,
    cause_description TEXT,
    incident_date DATE NOT NULL,
    incident_time TIME,
    date_precision TEXT NOT NULL DEFAULT 'day' CHECK (date_precision IN
        ('exact_datetime', 'day', 'month', 'year', 'approximate')),
    geom GEOMETRY(Point, 4326) NOT NULL,
    location_text TEXT,
    location_precision TEXT NOT NULL DEFAULT 'approximate' CHECK (location_precision IN
        ('exact', 'approximate', 'city_level', 'region_level', 'unknown')),
    country TEXT NOT NULL,
    admin_region TEXT,
    water_body TEXT,
    spill_type TEXT NOT NULL DEFAULT 'unknown' CHECK (spill_type IN (
        'tanker_collision', 'pipeline_leak', 'offshore_platform',
        'illegal_discharge', 'storage_facility_failure', 'grounding',
        'weather_related', 'unknown', 'other')),
    substance_type TEXT,
    volume_min_liters NUMERIC,
    volume_max_liters NUMERIC,
    volume_precision TEXT NOT NULL DEFAULT 'unknown' CHECK (volume_precision IN
        ('exact', 'estimated_range', 'order_of_magnitude', 'unknown')),
    area_affected_km2 NUMERIC,
    vessels_involved JSONB,
    status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN
        ('confirmed', 'suspected', 'unconfirmed', 'false_positive')),
    response_summary TEXT,
    confidence_level TEXT NOT NULL CHECK (confidence_level IN (
        'verified_official', 'verified_multi_source', 'reported_single_source',
        'probable', 'synthetic_demo')),
    is_synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    synthetic_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT historical_incident_synthetic_check CHECK (
        (is_synthetic = FALSE AND confidence_level != 'synthetic_demo') OR
        (is_synthetic = TRUE AND confidence_level = 'synthetic_demo' AND synthetic_notes IS NOT NULL)
    ),
    CONSTRAINT historical_incident_natural_key UNIQUE (title, incident_date, country)
);

CREATE INDEX IF NOT EXISTS idx_historical_incidents_geom ON incidents USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_historical_incidents_date ON incidents (incident_date);
CREATE INDEX IF NOT EXISTS idx_historical_incidents_country ON incidents (country);
CREATE INDEX IF NOT EXISTS idx_historical_incidents_synthetic ON incidents (is_synthetic);

CREATE TABLE IF NOT EXISTS incident_sources (
    incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    source_record_id TEXT,
    retrieved_at DATE NOT NULL DEFAULT CURRENT_DATE,
    excerpt_notes TEXT,
    PRIMARY KEY (incident_id, source_id)
);

CREATE OR REPLACE VIEW v_incident_summary AS
SELECT i.id, i.title, i.incident_date, i.country, i.admin_region,
       ST_Y(i.geom) AS latitude, ST_X(i.geom) AS longitude,
       i.location_precision, i.spill_type, i.substance_type,
       i.volume_min_liters, i.volume_max_liters, i.volume_precision,
       i.area_affected_km2, i.status, i.confidence_level, i.is_synthetic,
       (SELECT array_agg(s.name ORDER BY s.name)
          FROM incident_sources isr JOIN sources s ON s.id = isr.source_id
         WHERE isr.incident_id = i.id) AS source_names
FROM incidents i;