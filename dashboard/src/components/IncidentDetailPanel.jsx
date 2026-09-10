import { useEffect, useState } from "react";
import { getCostProjection, getSpillIncident } from "../lib/apiClient";
import { getSeverityColor, getPriorityColor, formatScore } from "../lib/spillIncidentHelpers";

export function IncidentDetailPanel({ spillId, onClose }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [costProjection, setCostProjection] = useState(null);

  useEffect(() => {
    let mounted = true;
    if (!spillId) return;

    const fetchDetail = async () => {
      setLoading(true);
      try {
        const res = await getSpillIncident(spillId);
        if (mounted) {
          setData(res);
          setLoading(false);
        }
      } catch (e) {
        console.error("Failed to fetch incident details", e);
        if (mounted) setLoading(false);
      }
      try {
        const projection = await getCostProjection(spillId);
        if (mounted) setCostProjection(projection);
      } catch (e) {
        if (mounted) setCostProjection(null);
      }
    };
    
    fetchDetail();
    // Refresh every 15s to get new recommendations/attribution
    const interval = setInterval(fetchDetail, 15000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [spillId]);

  if (!spillId) return null;

  return (
    <div className="incident-detail-overlay">
      <div className="incident-detail-panel">
        <div className="detail-header">
          <h2>Spill Incident {spillId.substring(0, 8)}</h2>
          <button className="close-button" onClick={onClose}>×</button>
        </div>
        
        {loading && !data ? (
          <div className="detail-content">Loading...</div>
        ) : (
          <div className="detail-content">
            <Section title="Overview">
              <Row label="Detected" value={new Date(data.incident.detected_at).toLocaleString()} />
              <Row label="Location" value={`${data.incident.latitude.toFixed(4)}, ${data.incident.longitude.toFixed(4)}`} />
              <Row label="Area" value={`${data.incident.area_km2?.toFixed(2)} km²`} />
              <Row label="Confidence" value={`${(data.incident.confidence * 100).toFixed(1)}%`} />
            </Section>

            {data.severity && (
              <Section title="Severity & Impact">
                <div style={{ display: "flex", gap: "10px", alignItems: "center", marginBottom: "10px" }}>
                  <span className="severity-badge" style={{ backgroundColor: getSeverityColor(data.severity.severity_level) }}>
                    {data.severity.severity_level}
                  </span>
                  <span>Score: {formatScore(data.severity.score)}</span>
                </div>
                <Row label="Coast Risk" value={formatScore(data.severity.coast_distance_risk)} />
                <Row label="Protected Area Risk" value={formatScore(data.severity.protected_area_risk)} />
                <Row label="Population Risk" value={formatScore(data.severity.population_risk)} />
              </Section>
            )}

            {data.attribution?.length > 0 && (
              <Section title="Top Suspects">
                <table className="suspect-table">
                  <thead>
                    <tr>
                      <th>Vessel</th>
                      <th>Type</th>
                      <th>Score</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.attribution.slice(0, 3).map((attr) => (
                      <tr key={attr.mmsi}>
                        <td>{attr.mmsi}</td>
                        <td>{attr.vessel_type || "Unknown"}</td>
                        <td>
                          <div className="score-bar-bg">
                            <div className="score-bar-fill" style={{ width: `${Math.min(100, attr.final_score * 100)}%` }}></div>
                          </div>
                          {(attr.final_score * 100).toFixed(0)}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Section>
            )}

            {data.forecasts?.length > 0 && (
              <Section title="Drift Forecasts">
                <div className="forecast-tags">
                  {data.forecasts.map(f => (
                    <span key={f.horizon_hours} className="forecast-tag">
                      +{f.horizon_hours}h
                    </span>
                  ))}
                </div>
              </Section>
            )}

            {costProjection && (
              <Section title="Response Cost Projection">
                <div className="cost-summary">
                  <strong>{costProjection.nosdcp_tier}</strong>
                  <span>USD {costProjection.low_usd.toLocaleString()} - {costProjection.high_usd.toLocaleString()}</span>
                </div>
                <div className="cost-chart" aria-label="Low to high response cost by forecast horizon">
                  {costProjection.cost_curve.map((point) => {
                    const ceiling = costProjection.high_usd || 1;
                    const width = Math.max(4, (point.high_usd / ceiling) * 100);
                    return (
                      <div className="cost-chart-row" key={point.horizon_hours}>
                        <span>+{point.horizon_hours}h</span>
                        <div className="cost-band-track">
                          <div className="cost-band" style={{ width: `${width}%` }}>
                            <span>USD {point.low_usd.toLocaleString()} - {point.high_usd.toLocaleString()}</span>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
                <small className="cost-note">{costProjection.volume_basis}</small>
                <Row label="Landfall estimate" value={costProjection.landfall_eta} />
                <Row label="Certified vessels" value={String(costProjection.matched_vessels?.length || 0)} />
              </Section>
            )}

            {costProjection?.matched_vessels?.length > 0 && (
              <Section title="Nearby Response Vessels">
                <div className="response-vessel-list">
                  {costProjection.matched_vessels.map((vessel, index) => (
                    <div className="response-vessel-card" key={vessel.mmsi || `${vessel.vessel_name}-${index}`}>
                      <div className="response-vessel-heading">
                        <strong>{vessel.vessel_name || "Unnamed response vessel"}</strong>
                        <span>{formatDistance(vessel.distance_m)}</span>
                      </div>
                      <Row label="Company" value={vessel.contractor_name || "Not provided"} />
                      <Row label="MMSI" value={vessel.mmsi || "Not provided"} />
                      <Row label="Certification" value={vessel.tier_rating || "Not provided"} />
                      <Row label="Equipment" value={vessel.equipment_summary || "Not provided"} />
                      <Row label="Location" value={formatCoordinates(vessel.latitude, vessel.longitude)} />
                      <Row label="AIS last seen" value={formatTimestamp(vessel.last_seen)} />
                      <ContactRow label="Phone" value={vessel.phone} href={vessel.phone ? `tel:${vessel.phone}` : null} />
                      <ContactRow label="Email" value={vessel.email} href={vessel.email ? `mailto:${vessel.email}` : null} />
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {data.recommendations?.length > 0 && (
              <Section title="Response Actions">
                <div className="recommendations-list">
                  {data.recommendations.map(rec => (
                    <div key={rec.id} className="recommendation-item">
                      <span className="priority-dot" style={{ backgroundColor: getPriorityColor(rec.priority) }}></span>
                      <span className="recommendation-text">{rec.recommendation}</span>
                    </div>
                  ))}
                </div>
              </Section>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="detail-section">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

function Row({ label, value }) {
  return (
    <div className="detail-row">
      <span className="detail-label">{label}:</span>
      <span className="detail-value">{value}</span>
    </div>
  );
}

function ContactRow({ label, value, href }) {
  return (
    <div className="detail-row">
      <span className="detail-label">{label}:</span>
      {href ? <a className="detail-contact" href={href}>{value}</a> : <span className="detail-value">Not provided</span>}
    </div>
  );
}

function formatDistance(distanceM) {
  const distance = Number(distanceM);
  if (!Number.isFinite(distance)) return "Distance unavailable";
  return distance < 1000 ? `${Math.round(distance)} m away` : `${(distance / 1000).toFixed(1)} km away`;
}

function formatCoordinates(latitude, longitude) {
  const lat = Number(latitude);
  const lon = Number(longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return "Not provided";
  return `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
}

function formatTimestamp(value) {
  if (!value) return "Not provided";
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? String(value) : timestamp.toLocaleString();
}
