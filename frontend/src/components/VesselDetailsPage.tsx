import React, { useEffect, useState } from "react";
import "./IncidentDetailsPage.css"; 

interface Props {
  vesselDetail: any;
  onBack: () => void;
}

export function VesselDetailsPage({ vesselDetail, onBack }: Props) {
  const [images, setImages] = useState<Record<string, string>>({});

  useEffect(() => {
    if (vesselDetail?.sar_tasking?.scene_id) {
      const sceneId = vesselDetail.sar_tasking.scene_id;
      const baseUrl = `http://${window.location.hostname}:8015/artifacts/` + sceneId;
      setImages({
        raw: baseUrl + "/raw_image.png",
        filtered: baseUrl + "/filtered_image.png",
        cfar: baseUrl + "/bright_target_mask.png",
        final: baseUrl + "/cleaned_mask.png",
      });
    }
  }, [vesselDetail]);

  if (!vesselDetail) {
    return (
      <div className="incident-details-page loading">
        <div className="spinner"></div>
        <p>Loading vessel intelligence...</p>
      </div>
    );
  }

  const { vessel, risk, sar_tasking } = vesselDetail;
  const riskTier = risk?.tier?.toLowerCase() || 'low';

  return (
    <div className="incident-details-page">
      <div className="idp-header">
        <div className="idp-header-left">
          <button className="idp-back-btn" onClick={onBack}>
            ← Back to Map
          </button>
          <div className="idp-title-group">
            <h1>Vessel {vessel.mmsi}</h1>
            <span className="idp-id-badge">{vessel.vessel_type || "Unknown"}</span>
            <span className={`idp-severity-badge ${riskTier}`}>
              RISK: {risk?.tier || "UNKNOWN"}
            </span>
          </div>
        </div>
        <div className="idp-header-right">
          <div className="idp-meta-item">
            <label>LAST COURSE</label>
            <span>{vessel.last_course ? `${vessel.last_course.toFixed(1)}°` : "N/A"}</span>
          </div>
          <div className="idp-meta-item">
            <label>RISK SCORE</label>
            <span>{risk?.risk_score?.toFixed(1) || "N/A"}</span>
          </div>
        </div>
      </div>

      <div className="idp-content">
        <div className="idp-sidebar">
          <div className="idp-card">
            <h3>Vessel Profile</h3>
            <div className="idp-stats-grid">
              <div className="idp-stat">
                <label>Name</label>
                <div className="idp-val">{vessel.name || "Unknown"}</div>
              </div>
              <div className="idp-stat">
                <label>MMSI</label>
                <div className="idp-val">{vessel.mmsi}</div>
              </div>
              <div className="idp-stat">
                <label>Type</label>
                <div className="idp-val">{vessel.vessel_type || "N/A"}</div>
              </div>
            </div>
          </div>

          <div className="idp-card">
            <h3>Risk Intelligence</h3>
            <div className="idp-stats-grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
              <div className="idp-stat">
                <label>Risk Score</label>
                <div className="idp-val">{risk?.risk_score?.toFixed(1) || "N/A"}</div>
              </div>
              <div className="idp-stat">
                <label>Recommended Action</label>
                <div className="idp-val" style={{ fontSize: '13px' }}>{risk?.recommended_action || "None"}</div>
              </div>
            </div>

            {risk?.contributing_factors && risk.contributing_factors.length > 0 && (
              <>
                <h3 style={{ marginTop: '12px' }}>Contributing Risk Factors</h3>
                <ul className="idp-attribution-list">
                  {risk.contributing_factors.map((f: any, idx: number) => (
                    <li key={idx} className="idp-attribution-item" style={{ borderLeftColor: f.contribution > 30 ? '#ef4444' : '#f97316' }}>
                      <div className="attr-main">
                        <span className="attr-rank">#{idx + 1}</span>
                        <span className="attr-name">{f.factor}</span>
                        <span className="attr-score">{f.contribution.toFixed(1)} pts</span>
                      </div>
                      <div className="attr-details">
                        <small>Weight: {f.weight.toFixed(2)}</small>
                        <small>Value: {f.value.toFixed(2)}</small>
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        </div>

        <div className="idp-main-view">
          {sar_tasking ? (
            <div className="idp-card full-height">
              <div className="sar-header-flex">
                <h3>SAR Processing Pipeline</h3>
                <span className="idp-id-badge">TASKING {sar_tasking.status?.toUpperCase()} | SCENE: {sar_tasking.scene_id}</span>
              </div>
              
              <div className="idp-sar-grid">
                <div className="sar-artifact">
                  <div className="sar-label">1. RAW SAR IMAGE</div>
                  <div className="sar-img-wrapper">
                    {images.raw ? (
                      <img src={images.raw} alt="Raw SAR" />
                    ) : <span className="sar-placeholder">PENDING</span>}
                  </div>
                </div>
                
                <div className="sar-artifact">
                  <div className="sar-label">2. DESPECKLED FILTER</div>
                  <div className="sar-img-wrapper">
                    {images.filtered ? (
                      <img src={images.filtered} alt="Despeckled" />
                    ) : <span className="sar-placeholder">PENDING</span>}
                  </div>
                </div>
                
                <div className="sar-artifact">
                  <div className="sar-label">3. CFAR OBJECT DETECTION</div>
                  <div className="sar-img-wrapper">
                    {images.cfar ? (
                      <img src={images.cfar} alt="CFAR Mask" />
                    ) : <span className="sar-placeholder">PENDING</span>}
                  </div>
                </div>
                
                <div className="sar-artifact">
                  <div className="sar-label">4. CLEANED POLYGON</div>
                  <div className="sar-img-wrapper">
                    {images.final ? (
                      <img src={images.final} alt="Polygon" />
                    ) : <span className="sar-placeholder">PENDING</span>}
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="idp-card full-height">
              <h3>SAR Imagery</h3>
              <div className="idp-empty-state centered">
                <span style={{ fontSize: '32px' }}>🛰️</span>
                <p>No SAR tasking has been requested for this vessel.</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
