export type Geometry = GeoJSON.Geometry;

export interface IncidentSummary {
  id: string;
  detected_at: string;
  latitude: number | null;
  longitude: number | null;
  area_km2: number | null;
  confidence: number | null;
  status: string;
  source: string | null;
  severity_level: string | null;
  severity_score: number | null;
  protected_area_risk: number | null;
  population_risk: number | null;
  top_attribution_score: number | null;
  top_vessel_mmsi: string | null;
  top_vessel_type: string | null;
}

export interface Vessel { mmsi: string; vessel_name: string | null; vessel_type: string | null; flag: string | null; last_lat: number | null; last_lon: number | null; last_seen: string | null; risk_score: number | null; risk_tier: string | null; heading_deg?: number | null; }
export interface DarkVessel { id: string; detected_at: string | null; latitude: number | null; longitude: number | null; sensor: string | null; confidence: number | null; matched_mmsi: string | null; matched_vessel_name?: string | null; }
export interface ProtectedArea { id: string; name: string | null; type: string | null; geometry: Geometry | null; }
export interface Forecast { id: number; horizon_hours: number; forecast_time: string | null; predicted_lat: number | null; predicted_lon: number | null; geometry: Geometry | null; generated_at: string | null; confidence: number | null; model_version: string | null; }
export interface Attribution { final_score: number | null; distance_score: number | null; trajectory_score: number | null; wind_score: number | null; time_score: number | null; behavior_score: number | null; mmsi: string | null; vessel_name: string | null; vessel_type: string | null; last_lat: number | null; last_lon: number | null; }
export interface Recommendation { id: number; recommendation: string; priority: string; status: string; generated_at: string | null; acknowledged_at: string | null; acknowledged_by: string | null; rationale?: string | null; }
export interface IncidentDetail { incident: IncidentSummary & { geometry: Geometry | null; source_image_id: string | null }; severity: Record<string, number | string | null> | null; attribution: Attribution[]; forecasts: Forecast[]; recommendations: Recommendation[]; intelligence_report?: unknown; }
