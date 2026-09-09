-- Idempotent response-decision cost and dispatch tables.

ALTER TABLE spill_incidents
    ADD COLUMN IF NOT EXISTS oil_type VARCHAR(100) NOT NULL DEFAULT 'medium_crude';

CREATE TABLE IF NOT EXISTS contractors (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    phone VARCHAR(100),
    email VARCHAR(255),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS certified_vessels (
    id BIGSERIAL PRIMARY KEY,
    mmsi VARCHAR(20) NOT NULL UNIQUE,
    contractor_id BIGINT NOT NULL REFERENCES contractors(id) ON DELETE CASCADE,
    tier_rating INTEGER NOT NULL DEFAULT 1 CHECK (tier_rating BETWEEN 1 AND 3),
    equipment_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_certified_vessels_contractor ON certified_vessels(contractor_id);

CREATE TABLE IF NOT EXISTS cost_projections (
    id BIGSERIAL PRIMARY KEY,
    spill_id UUID NOT NULL REFERENCES spill_incidents(id) ON DELETE CASCADE,
    nosdcp_tier VARCHAR(20) NOT NULL,
    estimated_volume_tonnes NUMERIC(14,3) NOT NULL,
    point_usd NUMERIC(14,2) NOT NULL,
    low_usd NUMERIC(14,2) NOT NULL,
    high_usd NUMERIC(14,2) NOT NULL,
    point_inr NUMERIC(16,2) NOT NULL,
    low_inr NUMERIC(16,2) NOT NULL,
    high_inr NUMERIC(16,2) NOT NULL,
    cost_curve JSONB NOT NULL,
    matched_vessels JSONB NOT NULL DEFAULT '[]'::jsonb,
    landfall_eta VARCHAR(20),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE cost_projections
    ADD COLUMN IF NOT EXISTS landfall_eta VARCHAR(20);
CREATE INDEX IF NOT EXISTS idx_cost_projections_spill_created
    ON cost_projections(spill_id, created_at DESC);

INSERT INTO contractors (name, phone, email) VALUES
    ('Sadhav Shipping', NULL, NULL),
    ('Lamor India', NULL, NULL),
    ('Sea Care Marine Services', NULL, NULL),
    ('Viraj Clean Sea', NULL, NULL)
ON CONFLICT (name) DO NOTHING;
