import { FusionResults } from "./FusionResults";
import React, { useEffect, useState } from "react";
import { SAR_ARTIFACT_FILENAMES, sarArtifactUrls, type SarArtifactKey } from "../lib/api";
import { SARArtifactPreview } from "./SARArtifactPreview";
import "./IncidentDetailsPage.css";
import { ARTIFACTS_BASE } from "../lib/api";
import { SARLookalikeAnalysis } from "./SARLookalikeAnalysis";
import { SourceAttributionAnalysis } from "./SourceAttributionAnalysis";
import { SpillForecastAnalysis } from "./SpillForecastAnalysis";

interface Props {
  incidentDetail: any; // Result from fetchIncidentDetail
  onBack: () => void;
  onSpotVessel?: (mmsi: string) => void;
  forecastHorizon: number;
  onForecastHorizonChange: (hours: number) => void;
}

function formatDistance(distanceM: unknown) {
  const distance = Number(distanceM);
  if (!Number.isFinite(distance)) return "Distance unavailable";
  return distance < 1000 ? `${Math.round(distance)} m away` : `${(distance / 1000).toFixed(1)} km away`;
}

function formatCoordinates(latitude: unknown, longitude: unknown) {
  const lat = Number(latitude);
  const lon = Number(longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return "Not provided";
  return `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
}

function formatTimestamp(value: unknown) {
  if (!value) return "Not provided";
  const timestamp = new Date(String(value));
  return Number.isNaN(timestamp.getTime()) ? String(value) : timestamp.toLocaleString();
}

function ResponseVesselCard({ vessel }: { vessel: any }) {
  return (
    <article className="idp-response-vessel">
      <div className="idp-response-vessel-head">
        <div>
          <strong>{vessel.vessel_name || "Unnamed response vessel"}</strong>
          <small>MMSI {vessel.mmsi || "not provided"}</small>
        </div>
        <span>{formatDistance(vessel.distance_m)}</span>
      </div>
      <dl className="idp-response-vessel-details">
        <div><dt>Company</dt><dd>{vessel.contractor_name || "Not provided"}</dd></div>
        <div><dt>Certification</dt><dd>{vessel.tier_rating || "Not provided"}</dd></div>
        <div><dt>Equipment</dt><dd>{vessel.equipment_summary || "Not provided"}</dd></div>
        <div><dt>Location</dt><dd>{formatCoordinates(vessel.latitude, vessel.longitude)}</dd></div>
        <div><dt>AIS last seen</dt><dd>{formatTimestamp(vessel.last_seen)}</dd></div>
        <div>
          <dt>Phone</dt>
          <dd>{vessel.phone ? <a href={`tel:${vessel.phone}`}>{vessel.phone}</a> : "Not provided"}</dd>
        </div>
        <div>
          <dt>Email</dt>
          <dd>{vessel.email ? <a href={`mailto:${vessel.email}`}>{vessel.email}</a> : "Not provided"}</dd>
        </div>
      </dl>
    </article>
  );
}

export function IncidentDetailsPage({ incidentDetail, onBack, onSpotVessel, forecastHorizon, onForecastHorizonChange }: Props) {
  const [images, setImages] = useState<Partial<Record<SarArtifactKey, string>>>({});

  const showCandidateImage = () => {
    document.getElementById("sar-candidate-evidence")?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  useEffect(() => {
    if (incidentDetail?.incident?.source_image_id) {
      setImages(sarArtifactUrls(incidentDetail.incident.source_image_id));
    } else {
      setImages({});
    }
  }, [incidentDetail]);

  if (!incidentDetail || !incidentDetail.incident) {
    return (
      <div className="incident-details-page loading">
        <div className="spinner"></div>
        <p>Loading incident intelligence...</p>
      </div>
    );
  }

  const { incident, severity, attribution, recommendations } = incidentDetail;
  const isSynthetic = incident.source === "synthetic";

  return (
    <div className="incident-details-page">
      {/* Header */}
      <header className="idp-header">
        <div className="idp-header-left">
          <button className="idp-back-btn" onClick={onBack}>
            ← Back to Map
          </button>
          <div className="idp-title-group">
            <h1>{isSynthetic ? "Synthetic Slick" : "Surface Anomaly"}</h1>
            <span className="idp-id-badge" title={incident.id}>Spill ID: {incident.id}</span>
            <span className={`idp-severity-badge ${severity?.severity_level?.toLowerCase() || 'low'}`}>
              {severity?.severity_level || 'LOW'} SEVERITY
            </span>
          </div>
        </div>
        <div className="idp-header-right">
          <div className="idp-meta-item">
            <label>COORDINATES</label>
            <span>{incident.latitude.toFixed(4)}N, {incident.longitude.toFixed(4)}E</span>
          </div>
          <div className="idp-meta-item">
            <label>DETECTED AT</label>
            <span>{new Date(incident.detected_at).toLocaleString()}</span>
          </div>
        </div>
      </header>

      {/* Main Content Grid */}
      <div className="idp-content">
        
        {/* Left Column: Context & Intelligence */}
        <div className="idp-sidebar">
          
          <section className="idp-card">
            <h3>Impact Analysis</h3>
            <div className="idp-stats-grid">
              <div className="idp-stat">
                <label>Estimated Area</label>
                <div className="idp-val">{incident.area_km2.toFixed(2)} km²</div>
              </div>
              <div className="idp-stat">
                <label>Detection Confidence</label>
                <div className="idp-val" style={{ color: incident.confidence > 0.8 ? '#34d399' : '#f59e0b' }}>
                  {Math.round(incident.confidence * 100)}%
                </div>
              </div>
              <div className="idp-stat">
                <label>Ecological Exposure</label>
                <div className="idp-val">{severity?.protected_area_risk > 0 ? "High" : "Low"}</div>
              </div>
            </div>
          </section>

          <SourceAttributionAnalysis attribution={attribution} incident={incident} onSpotVessel={onSpotVessel} />

          <SpillForecastAnalysis incidentDetail={incidentDetail} horizon={forecastHorizon} onHorizonChange={onForecastHorizonChange} />

          <section className="idp-card">
            <h3>Recommended Actions</h3>
            {recommendations && recommendations.length > 0 ? (
              <ul className="idp-action-list">
                {recommendations.map((rec: any, idx: number) => (
                  <li key={idx} className={`idp-action-item priority-${rec.priority.toLowerCase()}`}>
                    <div className="action-dot"></div>
                    <p>{rec.recommendation}</p>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="idp-empty-state">No recommended actions generated yet.</div>
            )}
          </section>

          <SARLookalikeAnalysis incidentDetail={incidentDetail} onShowCandidate={showCandidateImage} />
          <section className="idp-card">
            <h3>Response Cost Estimate</h3>
            {incidentDetail.cost_projection ? <>
              <p>{incidentDetail.cost_projection.nosdcp_tier} · USD {Number(incidentDetail.cost_projection.point_usd).toLocaleString()}</p>
              <p>Range: USD {Number(incidentDetail.cost_projection.low_usd).toLocaleString()}–{Number(incidentDetail.cost_projection.high_usd).toLocaleString()}</p>
              <small>{incidentDetail.cost_projection.volume_basis}</small>
              <div className="idp-response-summary">
                <span>{incidentDetail.cost_projection.matched_vessels?.length || 0} certified response vessels matched</span>
                <span>Landfall estimate: {incidentDetail.cost_projection.landfall_eta || "Unavailable"}</span>
              </div>
              {incidentDetail.cost_projection.matched_vessels?.length > 0 ? (
                <div className="idp-response-vessels">
                  {incidentDetail.cost_projection.matched_vessels.map((vessel: any, index: number) => (
                    <ResponseVesselCard key={vessel.mmsi || `${vessel.vessel_name || "vessel"}-${index}`} vessel={vessel} />
                  ))}
                </div>
              ) : (
                <div className="idp-empty-state">No certified response vessels were matched within the configured search radius.</div>
              )}
            </> : <p>Cost estimate not yet available.</p>}
          </section>

        </div>

        {/* Right Column: SAR Imagery */}
        <div className="idp-main-view">
          <section className="idp-card full-height" id="sar-candidate-evidence">
            <div className="sar-header-flex">
              <h3>SAR Processing Pipeline Evidence</h3>
              {incident.source_image_id && (
                <span className="sar-scene-id" style={{fontSize: "12px", color: "#9ca3af"}}>Scene: {incident.source_image_id}</span>
              )}
            </div>
            
            {incidentDetail.fusion_metadata ? <FusionResults result={incidentDetail.fusion_metadata} /> : Object.keys(images).length > 0 ? (
              <div className="idp-sar-grid">
                <div className="sar-artifact">
                  <div className="sar-label">1. Raw Satellite Feed (Sentinel-1 VV)</div>
                  <div className="sar-img-wrapper">
                    <SARArtifactPreview url={images.raw} alt="Raw SAR" filename={SAR_ARTIFACT_FILENAMES.raw} />
                  </div>
                </div>
                <div className="sar-artifact">
                  <div className="sar-label">2. Despeckled Filter (Lee)</div>
                  <div className="sar-img-wrapper">
                    <SARArtifactPreview url={images.filtered} alt="Despeckled" filename={SAR_ARTIFACT_FILENAMES.filtered} />
                  </div>
                </div>
                <div className="sar-artifact">
                  <div className="sar-label">3. Adaptive Threshold (CFAR)</div>
                  <div className="sar-img-wrapper">
                    <SARArtifactPreview url={images.cfar} alt="CFAR Mask" filename={SAR_ARTIFACT_FILENAMES.cfar} />
                  </div>
                </div>
                <div className="sar-artifact">
                  <div className="sar-label">4. Final Polygon Extraction</div>
                  <div className="sar-img-wrapper">
                    <SARArtifactPreview url={images.final} alt="Final Polygon" filename={SAR_ARTIFACT_FILENAMES.final} />
                  </div>
                </div>
              </div>
            ) : (
              <div className="idp-empty-state centered">
                <p>No SAR imagery artifacts found for this incident.</p>
                <small>This might be an older incident where artifacts were not preserved, or processing is still underway.</small>
              </div>
            )}
          </section>
        </div>

      </div>
    </div>
  );
}
