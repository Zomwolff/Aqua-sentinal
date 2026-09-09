-- ============================================================================
-- Severity-Impact V1 Migration
-- Implements rule-based severity with horizon/footprint awareness
-- Changes score range from [0,1] to [0,100]
-- ============================================================================

-- Add new columns for V1 methodology
ALTER TABLE severity ADD COLUMN IF NOT EXISTS horizon_hours NUMERIC(6,2);
ALTER TABLE severity ADD COLUMN IF NOT EXISTS footprint_type TEXT CHECK (footprint_type IN ('best_estimate', 'probability_90', 'current'));
ALTER TABLE severity ADD COLUMN IF NOT EXISTS methodology_version VARCHAR(50) DEFAULT 'severity-impact-v1';

-- Peak severity tracking
ALTER TABLE severity ADD COLUMN IF NOT EXISTS peak_severity severity_level_enum;
ALTER TABLE severity ADD COLUMN IF NOT EXISTS peak_horizon_hours NUMERIC(6,2);
ALTER TABLE severity ADD COLUMN IF NOT EXISTS peak_footprint_type TEXT;
ALTER TABLE severity ADD COLUMN IF NOT EXISTS peak_score NUMERIC(5,2);

-- Trajectory
ALTER TABLE severity ADD COLUMN IF NOT EXISTS trajectory TEXT CHECK (trajectory IN ('IMPROVING', 'STABLE', 'WORSENING'));

-- Domain-level results
ALTER TABLE severity ADD COLUMN IF NOT EXISTS ecological_severity TEXT CHECK (ecological_severity IN ('None', 'Low', 'Medium', 'High', 'Critical'));
ALTER TABLE severity ADD COLUMN IF NOT EXISTS socioeconomic_severity TEXT CHECK (socioeconomic_severity IN ('None', 'Low', 'Medium', 'High', 'Critical'));

-- Escalation tracking
ALTER TABLE severity ADD COLUMN IF NOT EXISTS escalation_applied BOOLEAN DEFAULT FALSE;
ALTER TABLE severity ADD COLUMN IF NOT EXISTS escalation_reasons TEXT[];

-- Primary drivers (JSONB for flexibility)
ALTER TABLE severity ADD COLUMN IF NOT EXISTS primary_drivers JSONB;

-- Physical/exposure diagnostics
ALTER TABLE severity ADD COLUMN IF NOT EXISTS physical_area_m2 NUMERIC(14,2);
ALTER TABLE severity ADD COLUMN IF NOT EXISTS drift_distance_m NUMERIC(10,1);
ALTER TABLE severity ADD COLUMN IF NOT EXISTS expansion_ratio NUMERIC(8,4);

-- Ecological detail (preserved from ecological_impact)
ALTER TABLE severity ADD COLUMN IF NOT EXISTS ttfe_hours NUMERIC(6,2);
ALTER TABLE severity ADD COLUMN IF NOT EXISTS affected_receptor_count INT;

-- Socioeconomic detail
ALTER TABLE severity ADD COLUMN IF NOT EXISTS ports_within_5km INT DEFAULT 0;
ALTER TABLE severity ADD COLUMN IF NOT EXISTS fishing_zones_within_10km INT DEFAULT 0;

-- Change score range from [0,1] to [0,100]
ALTER TABLE severity DROP CONSTRAINT IF EXISTS severity_score_check;
ALTER TABLE severity ALTER COLUMN score TYPE NUMERIC(5,2);
ALTER TABLE severity ADD CONSTRAINT severity_score_check CHECK (score BETWEEN 0 AND 100);

-- Migrate existing data (multiply by 100 if values are in [0,1] range)
UPDATE severity 
SET score = score * 100 
WHERE score <= 1.0;

-- Add unique constraint for idempotency (spill + horizon + footprint)
-- Drop old data first to avoid conflicts
CREATE UNIQUE INDEX IF NOT EXISTS idx_severity_unique_assessment 
    ON severity(spill_id, COALESCE(horizon_hours, 0), COALESCE(footprint_type, 'current'));

-- Add index for horizon queries
CREATE INDEX IF NOT EXISTS idx_severity_horizon ON severity(horizon_hours);
CREATE INDEX IF NOT EXISTS idx_severity_footprint ON severity(footprint_type);

-- Add comments
COMMENT ON COLUMN severity.score IS 'Severity score [0-100]. Low=0-24, Moderate=25-49, High=50-74, Critical=75-100. Score is secondary to severity_level.';
COMMENT ON COLUMN severity.horizon_hours IS 'Forecast horizon (NULL for current/overall severity). Values: 1.0, 3.0, 6.0, 12.0, 24.0';
COMMENT ON COLUMN severity.footprint_type IS 'best_estimate | probability_90 | current. Current represents overall severity.';
COMMENT ON COLUMN severity.ecological_severity IS 'Maximum ecological receptor category across all receptor types.';
COMMENT ON COLUMN severity.socioeconomic_severity IS 'Socioeconomic severity based on port/fishing zone proximity (V1: no population data).';
COMMENT ON COLUMN severity.escalation_applied IS 'TRUE if rule-based escalation increased severity tier.';
COMMENT ON COLUMN severity.escalation_reasons IS 'Array of escalation reasons: multiple_high_ecological_receptors, cross_domain_escalation, ttfe_urgency';
COMMENT ON COLUMN severity.trajectory IS 'Severity trend: current vs 24h probability_90.';
COMMENT ON COLUMN severity.ttfe_hours IS 'Time-to-first-exposure: earliest horizon with exposure > 0 (from ecological_impact).';
COMMENT ON COLUMN severity.methodology_version IS 'Severity calculation methodology version.';

-- Verify migration
DO $$
BEGIN
    RAISE NOTICE 'migrate_severity_v1.sql: Severity-Impact V1 schema migration complete.';
END $$;
