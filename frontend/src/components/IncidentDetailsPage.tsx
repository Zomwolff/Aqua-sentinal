import React, { useEffect, useState } from "react";
import { SAR_ARTIFACT_FILENAMES, sarArtifactUrls, type SarArtifactKey } from "../lib/api";
import { SARArtifactPreview } from "./SARArtifactPreview";
import "./IncidentDetailsPage.css";

interface Props {
  incidentDetail: any; // Result from fetchIncidentDetail
  onBack: () => void;
}

export function IncidentDetailsPage({ incidentDetail, onBack }: Props) {
  const [images, setImages] = useState<Partial<Record<SarArtifactKey, string>>>({});

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
            <span className="idp-id-badge">{incident.id.substring(0, 8)}</span>
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

          <section className="idp-card">
            <h3>Source Attribution</h3>
            {attribution && attribution.length > 0 ? (
              <ul className="idp-attribution-list">
                {attribution.map((attr: any, idx: number) => (
                  <li key={idx} className="idp-attribution-item">
                    <div className="attr-main">
                      <span className="attr-rank">#{idx + 1}</span>
                      <span className="attr-name">{attr.vessel_name || `MMSI ${attr.mmsi}`}</span>
                      <span className="attr-score">{(attr.final_score * 100).toFixed(1)}% Match</span>
                    </div>
                    <div className="attr-details">
                      <small>Distance: {(attr.distance_score * 100).toFixed(0)}%</small>
                      <small>Trajectory: {(attr.trajectory_score * 100).toFixed(0)}%</small>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="idp-empty-state">No nearby vessels correlated.</div>
            )}
          </section>

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

        </div>

        {/* Right Column: SAR Imagery */}
        <div className="idp-main-view">
          <section className="idp-card full-height">
            <div className="sar-header-flex">
              <h3>SAR Processing Pipeline Evidence</h3>
              {incident.source_image_id && (
                <span className="sar-scene-id" style={{fontSize: "12px", color: "#9ca3af"}}>Scene: {incident.source_image_id}</span>
              )}
            </div>
            
            {Object.keys(images).length > 0 ? (
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
