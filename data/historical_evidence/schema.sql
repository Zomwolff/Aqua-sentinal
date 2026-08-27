-- ============================================================================
-- Historical Oil Spill Incident Layer — PostGIS schema
-- ============================================================================
-- Design goals:
--   1. Every incident is traceable to at least one named, dated source record.
--   2. Confidence/provenance is a first-class field, not an afterthought.
--   3. Synthetic/demo records are structurally impossible to confuse with
--      real ones (separate boolean + mandatory notes + separate source row).
--   4. Location and volume precision are stored explicitly, since most
--      historical spill reports do NOT give exact figures.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ----------------------------------------------------------------------------
-- Lookup: sources / provenance registry
-- ----------------------------------------------------------------------------
CREATE TABLE sources (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                TEXT NOT NULL,                 -- e.g. "NOAA ORR Incident Data"
    source_type         TEXT NOT NULL CHECK (source_type IN (
                            'official_government',
                            'industry_ngo',             -- e.g. ITOPF, Cedre
                            'academic_dataset',
                            'news_media',
                            'satellite_detection',       -- EMSA CleanSeaNet etc.
                            'crowd_wiki',                -- Wikipedia etc.
                            'synthetic_demo'             -- explicitly fabricated for demo coverage
                         )),
    url                 TEXT,
    license             TEXT,
    -- Rough trust tier used as a DEFAULT confidence hint (can be overridden per-incident).
    -- 1 = official/primary record, 2 = peer-reviewed/curated dataset,
    -- 3 = reputable news reporting, 4 = crowd-sourced/wiki, 5 = synthetic/demo.
    reliability_tier    SMALLINT NOT NULL CHECK (reliability_tier BETWEEN 1 AND 5),
    last_verified_at    DATE,
    notes               TEXT
);

-- ----------------------------------------------------------------------------
-- Core incident table
-- ----------------------------------------------------------------------------
CREATE TABLE incidents (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Identity / narrative
    title                   TEXT NOT NULL,              -- short human label, e.g. "2017 Ennore Oil Spill"
    description             TEXT,                       -- free-text summary, own words, cite sources externally
    cause_description       TEXT,                       -- e.g. "Collision between LPG tanker and oil tanker"

    -- Temporal
    incident_date           DATE NOT NULL,
    incident_time           TIME,                       -- nullable, many records only have a date
    date_precision          TEXT NOT NULL DEFAULT 'day' CHECK (date_precision IN
                                ('exact_datetime','day','month','year','approximate')),

    -- Spatial
    geom                    GEOMETRY(Point, 4326) NOT NULL,
    location_text           TEXT,                       -- e.g. "12 km off Kamarajar Port, Ennore"
    location_precision      TEXT NOT NULL DEFAULT 'approximate' CHECK (location_precision IN
                                ('exact','approximate','city_level','region_level','unknown')),
    country                 TEXT NOT NULL,              -- ISO-ish country name, derived ONLY from source data
    admin_region            TEXT,                       -- state/province, e.g. "Tamil Nadu"
    water_body              TEXT,                       -- e.g. "Bay of Bengal", "Arabian Sea"

    -- Spill characteristics
    spill_type              TEXT NOT NULL DEFAULT 'unknown' CHECK (spill_type IN (
                                'tanker_collision','pipeline_leak','offshore_platform',
                                'illegal_discharge','storage_facility_failure',
                                'grounding','weather_related','unknown','other')),
    substance_type           TEXT,                      -- e.g. "crude_oil","diesel","HFO","lube_oil","unknown"
    volume_min_liters        NUMERIC,
    volume_max_liters        NUMERIC,
    volume_precision         TEXT NOT NULL DEFAULT 'unknown' CHECK (volume_precision IN
                                ('exact','estimated_range','order_of_magnitude','unknown')),
    area_affected_km2        NUMERIC,

    -- Vessels / source assets involved (kept flexible; normalize into a
    -- vessels table later once you have enough records to dedupe by IMO number)
    vessels_involved         JSONB,                     -- [{"name": "...", "imo": "...", "role": "source|struck|other"}]

    -- Status & lifecycle
    status                   TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN
                                ('confirmed','suspected','unconfirmed','false_positive')),
    response_summary         TEXT,

    -- --- Confidence / provenance / synthetic-flagging (the important part) ---
    confidence_level         TEXT NOT NULL CHECK (confidence_level IN (
                                'verified_official',     -- govt/agency primary record
                                'verified_multi_source',  -- corroborated by 2+ independent reputable sources
                                'reported_single_source', -- one news/report source, uncorroborated
                                'probable',               -- inferred/estimated, gaps in record
                                'synthetic_demo'          -- NOT a real incident — see is_synthetic
                             )),
    is_synthetic             BOOLEAN NOT NULL DEFAULT FALSE,
    synthetic_notes          TEXT,                       -- REQUIRED (enforced below) if is_synthetic = TRUE

    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT synthetic_must_be_flagged CHECK (
        (is_synthetic = FALSE AND confidence_level != 'synthetic_demo')
        OR
        (is_synthetic = TRUE AND confidence_level = 'synthetic_demo' AND synthetic_notes IS NOT NULL)
    )
);

CREATE INDEX idx_incidents_geom ON incidents USING GIST (geom);
CREATE INDEX idx_incidents_date ON incidents (incident_date);
CREATE INDEX idx_incidents_country ON incidents (country);
CREATE INDEX idx_incidents_is_synthetic ON incidents (is_synthetic);

-- ----------------------------------------------------------------------------
-- Join table: which source(s) back each incident (many-to-many)
-- An incident can (and ideally should) be corroborated by multiple sources.
-- ----------------------------------------------------------------------------
CREATE TABLE incident_sources (
    incident_id         UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    source_id           UUID NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    source_record_id    TEXT,           -- the ID/URL this record has *within* that source, if any
    retrieved_at         DATE NOT NULL DEFAULT CURRENT_DATE,
    excerpt_notes        TEXT,          -- paraphrased notes only — never verbatim reproduction of copyrighted text
    PRIMARY KEY (incident_id, source_id)
);

-- ----------------------------------------------------------------------------
-- Convenience view: what the frontend's "Historical Context" panel wants
-- ----------------------------------------------------------------------------
CREATE VIEW v_incident_summary AS
SELECT
    i.id,
    i.title,
    i.incident_date,
    i.country,
    i.admin_region,
    ST_Y(i.geom) AS latitude,
    ST_X(i.geom) AS longitude,
    i.location_precision,
    i.spill_type,
    i.substance_type,
    i.volume_min_liters,
    i.volume_max_liters,
    i.volume_precision,
    i.status,
    i.confidence_level,
    i.is_synthetic,
    (SELECT array_agg(s.name) FROM incident_sources isr
        JOIN sources s ON s.id = isr.source_id
        WHERE isr.incident_id = i.id) AS source_names
FROM incidents i;

-- ----------------------------------------------------------------------------
-- Example query the backend will actually run a lot:
-- "incidents within N km of a candidate point, most recent first"
-- ----------------------------------------------------------------------------
-- SELECT *, ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) / 1000 AS distance_km
-- FROM incidents
-- WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :radius_meters)
-- ORDER BY distance_km ASC, incident_date DESC;
