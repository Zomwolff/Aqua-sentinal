-- ============================================================================
-- Oil Spread Component V2 Migration
-- Adds separated drift, physical spreading, and uncertainty metrics
-- ============================================================================

-- Add drift metrics
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    drift_distance_m NUMERIC(10,1);
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    drift_velocity_ms NUMERIC(8,3);
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    drift_bearing_deg NUMERIC(6,2);

-- Add physical spreading metrics
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    physical_area_m2 NUMERIC(14,2);
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    physical_radius_m NUMERIC(10,1);
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    expansion_ratio NUMERIC(8,4);
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    spread_rate_m2_per_hour NUMERIC(12,2);

-- Add uncertainty metrics
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    uncertainty_rms_m NUMERIC(10,1);
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    uncertainty_std_east_m NUMERIC(10,1);
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    uncertainty_std_north_m NUMERIC(10,1);

-- Add probability contour geometries
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    probability_50_geom GEOMETRY(Polygon, 4326);
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    probability_90_geom GEOMETRY(Polygon, 4326);
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    convex_hull_geom GEOMETRY(Polygon, 4326);

-- Add oil properties metadata (JSONB for flexibility)
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    oil_properties JSONB;

-- Add windage metadata
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    windage_range NUMERIC(5,4)[];

-- Add degraded flag for missing forcing
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS
    forcing_degraded BOOLEAN DEFAULT FALSE;

-- Create indexes for new geometry columns
CREATE INDEX IF NOT EXISTS idx_forecasts_prob50_geom 
    ON forecasts USING GIST (probability_50_geom);
CREATE INDEX IF NOT EXISTS idx_forecasts_prob90_geom 
    ON forecasts USING GIST (probability_90_geom);

-- Add comments for clarity
COMMENT ON COLUMN forecasts.geom IS 
    'V2: Best-estimate physical oil footprint (physical spreading at drift-advected centroid)';
COMMENT ON COLUMN forecasts.probability_50_geom IS 
    '50% probability contour (forecast uncertainty envelope)';
COMMENT ON COLUMN forecasts.probability_90_geom IS 
    '90% probability contour (forecast uncertainty envelope)';
COMMENT ON COLUMN forecasts.convex_hull_geom IS 
    'Convex hull of particle ensemble (visualization reference, NOT a probability contour)';
COMMENT ON COLUMN forecasts.drift_distance_m IS 
    'Displacement of centroid from initial position (metres)';
COMMENT ON COLUMN forecasts.physical_area_m2 IS 
    'Physical oil slick area from Fay spreading model (m²)';
COMMENT ON COLUMN forecasts.expansion_ratio IS 
    'Area expansion ratio: A(t) / A(0)';
COMMENT ON COLUMN forecasts.uncertainty_rms_m IS 
    'RMS particle dispersion - represents forecast uncertainty, NOT physical oil radius';
