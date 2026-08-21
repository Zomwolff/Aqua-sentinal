import React, { useEffect, useState } from "react";
import "./IncidentDetailsPage.css";
import { fetchIncidentDetail, SAR_ARTIFACT_FILENAMES, sarArtifactUrls, type SarArtifactKey } from "../lib/api";
import { SARArtifactPreview } from "./SARArtifactPreview";

interface Props {
  vesselDetail: any;
  onBack: () => void;
  liveEvent?: any;
}

const SAR_STEPS = [
  { id: "sar_tasking", label: "Acquiring SAR", detail: "Sentinel-1 scene selected over the vessel's position" },
  { id: "sar_fetching", label: "Downloading Scene", detail: "GeoTIFF download via Google Earth Engine" },
  { id: "sar_despeckling", label: "Despeckling Filter", detail: "Lee filter in linear-power domain" },
  { id: "sar_cfar", label: "CFAR Object Detection", detail: "Dark-pixel anomaly detection" },
  { id: "sar_morphology", label: "Morphological Cleaning", detail: "Opening/closing of the candidate mask" },
  { id: "sar_polygonize", label: "Polygon Extraction", detail: "Connected components to spill polygons" },
  { id: "sar_complete", label: "Spill Processing Complete", detail: "Candidates persisted and fused" },
];

export function VesselDetailsPage({ vesselDetail, onBack, liveEvent }: Props) {
  const [images, setImages] = useState<Partial<Record<SarArtifactKey, string>>>({});
  const [liveStep, setLiveStep] = useState<string | null>(null);
  const [cacheBuster, setCacheBuster] = useState<number>(Date.now());
  const [incidentReport, setIncidentReport] = useState<any>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);

  useEffect(() => {
    const tasking = vesselDetail?.sar_tasking;
    if (tasking?.scene_id) {
      setImages(sarArtifactUrls(tasking.scene_id));
    }
  }, [vesselDetail]);

  // Track pipeline progress for THIS vessel's scene while it streams.
  useEffect(() => {
    if (!liveEvent || liveEvent.type !== "sar_tasking") return;
    if (vesselDetail?.sar_tasking && liveEvent.data?.scene_id === vesselDetail.sar_tasking.scene_id) {
      setLiveStep(liveEvent.data.step);
      // Force reload images by updating the cacheBuster, which will be appended to URLs
      setCacheBuster(Date.now());
    }
  }, [liveEvent, vesselDetail]);

  // Once this vessel is attributed to a spill, load the backend-computed
  // intelligence report for the independently scrollable left rail.
  useEffect(() => {
    const spillId = vesselDetail?.verdict?.spill_id;
    let cancelled = false;
    setIncidentReport(null);
    setReportError(null);
    if (!spillId) return;

    setReportLoading(true);
    fetchIncidentDetail(String(spillId))
      .then((detail) => {
        if (!cancelled) setIncidentReport(detail?.intelligence_report || null);
      })
      .catch((error) => {
        if (!cancelled) setReportError(error instanceof Error ? error.message : "Incident report unavailable");
      })
      .finally(() => {
        if (!cancelled) setReportLoading(false);
      });

    return () => { cancelled = true; };
  }, [vesselDetail?.verdict?.spill_id]);

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
  const previewPendingLabel = sar_tasking?.status === "failed" ? "PROCESSING FAILED" : "PENDING";

  // Honest step state derivation: fulfilled -> all done; failed -> stopped;
  // pending -> acquisition active; otherwise follow the live stream.
  const isFulfilled = sar_tasking?.status === "fulfilled" || liveStep === "sar_complete";
  const isFailed = sar_tasking?.status === "failed";
  const currentIdx = (() => {
    if (isFulfilled) return SAR_STEPS.length - 1;
    if (isFailed) return -1;
    if (liveStep) {
      const idx = SAR_STEPS.findIndex((s) => s.id === liveStep);
      if (idx >= 0) return idx;
    }
    if (sar_tasking) return 0; // pending: awaiting acquisition
    return -1;
  })();

  const reasonFactors: any[] = Array.isArray(sar_tasking?.reason?.contributing_factors)
    ? sar_tasking.reason.contributing_factors
    : [];

  const stageText = isFailed
    ? "Acquisition failed — no mock fallback configured; a new request will be issued on next risk recomputation"
    : isFulfilled
      ? "Scene processed"
      : sar_tasking
        ? `Stage ${currentIdx + 1} of ${SAR_STEPS.length}: ${SAR_STEPS[currentIdx]?.label}`
        : "";

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

          <div className="idp-card">
            <h3>Final Verdict</h3>
            {vesselDetail.verdict?.status === "spill_detected" ? (
              <div style={{ padding: '16px', background: 'rgba(237, 104, 76, 0.1)', border: '1px solid rgba(237, 104, 76, 0.3)', borderRadius: '4px', marginTop: '10px' }}>
                <div style={{ color: "#ed684c", fontWeight: 700, fontSize: "16px", marginBottom: "4px" }}>OIL SPILL DETECTED</div>
                <div style={{ color: "#e8ede7", fontSize: "12px", opacity: 0.8 }}>Spill ID: {vesselDetail.verdict.spill_id}</div>
              </div>
            ) : vesselDetail.verdict?.status === "no_spill_detected" ? (
              <div style={{ padding: '16px', background: 'rgba(118, 188, 153, 0.1)', border: '1px solid rgba(118, 188, 153, 0.3)', borderRadius: '4px', marginTop: '10px' }}>
                <div style={{ color: "#76bc99", fontWeight: 700, fontSize: "16px", marginBottom: "4px" }}>CLEAR (NO SPILL)</div>
                <div style={{ color: "#e8ede7", fontSize: "12px", opacity: 0.8 }}>Tasking completed, no spill detected in ROI.</div>
              </div>
            ) : vesselDetail.verdict?.status === "pending" ? (
              <div style={{ padding: '16px', background: 'rgba(229, 183, 93, 0.1)', border: '1px solid rgba(229, 183, 93, 0.3)', borderRadius: '4px', marginTop: '10px' }}>
                <div style={{ color: "#e5b75d", fontWeight: 700, fontSize: "16px", marginBottom: "4px" }}>SAR PENDING</div>
                <div style={{ color: "#e8ede7", fontSize: "12px", opacity: 0.8 }}>
                  {sar_tasking?.requested_at
                    ? `Tasking requested ${new Date(sar_tasking.requested_at).toLocaleTimeString("en-US", { hour12: false, timeZone: "UTC" })} UTC · awaiting Sentinel-1 revisit (orbit-dependent)`
                    : "Awaiting satellite tasking completion."}
                </div>
                {reasonFactors.length > 0 && (
                  <ul style={{ margin: "8px 0 0", paddingLeft: "16px", color: "#e8ede7", fontSize: "11px", opacity: 0.85 }}>
                    {reasonFactors.map((f: any, i: number) => (
                      <li key={i}><b>{String(f.factor).replace(/_/g, " ")}</b>{f.contribution != null ? ` — contributed ${Number(f.contribution).toFixed(1)} pts` : ""}</li>
                    ))}
                  </ul>
                )}
              </div>
            ) : (
              <div style={{ padding: '16px', background: 'rgba(255, 255, 255, 0.05)', border: '1px solid rgba(255, 255, 255, 0.1)', borderRadius: '4px', marginTop: '10px' }}>
                <div style={{ color: "#a0a0a0", fontWeight: 700, fontSize: "16px" }}>NO TASKING REQUESTED</div>
                <div style={{ color: "#8a968f", fontSize: "12px", opacity: 0.8 }}>No SAR tasking has been requested for this vessel.</div>
              </div>
            )}
          </div>

          <div className="idp-card incident-intelligence-card">
            <h3>Oil Spill Incident</h3>
            {vesselDetail.verdict?.status === "no_spill_detected" ? (
              <>
                <div className="incident-summary-grid">
                  <div><label>Severity</label><strong className="report-low">NONE</strong></div>
                  <div><label>Detection Result</label><strong>CLEAR</strong></div>
                  <div><label>Detected Spill Area</label><strong>0.00 km²</strong></div>
                  <div><label>Ecological Risk</label><strong>NOT ASSESSED</strong></div>
                </div>

                <div className="incident-report-section">
                  <h4>Probable Source</h4>
                  <div className="idp-empty-state">Not applicable — no spill candidate was detected.</div>
                </div>

                <div className="incident-report-section">
                  <h4>Spill Forecast</h4>
                  <div className="idp-empty-state">Not generated — there is no detected spill geometry to forecast.</div>
                </div>

                <div className="incident-report-section">
                  <h4>Recommended Actions</h4>
                  <ol className="report-action-list">
                    <li>Continue routine AIS monitoring for this vessel.</li>
                    <li>Request follow-up SAR only if the vessel risk remains elevated or new evidence appears.</li>
                  </ol>
                </div>

                <small className="report-provenance">Derived from the completed SAR tasking result; no spill evidence was detected in the monitored area.</small>
              </>
            ) : vesselDetail.verdict?.status !== "spill_detected" ? (
              <div className="idp-empty-state">Incident intelligence will appear after SAR processing is completed.</div>
            ) : reportLoading ? (
                <div className="idp-empty-state">Calculating incident intelligence…</div>
              ) : reportError ? (
                <div className="idp-empty-state">{reportError}</div>
              ) : incidentReport ? (
                <>
                  <div className="incident-summary-grid">
                    <div><label>Severity</label><strong className={`report-${String(incidentReport.assessment?.severity || "unknown").toLowerCase()}`}>{incidentReport.assessment?.severity || "UNKNOWN"}</strong></div>
                    <div><label>Confidence</label><strong>{incidentReport.assessment?.detection_confidence_percent ?? "—"}%</strong></div>
                    <div><label>Spill Area</label><strong>{incidentReport.assessment?.spill_area_km2 != null ? `${Number(incidentReport.assessment.spill_area_km2).toFixed(2)} km²` : "—"}</strong></div>
                    <div><label>Ecological Risk</label><strong>{incidentReport.assessment?.ecological_risk?.level || "UNKNOWN"}</strong></div>
                  </div>

                  <div className="incident-report-section">
                    <h4>Probable Source</h4>
                    <div className="source-share-list">
                      {(incidentReport.probable_sources || []).map((source: any, index: number) => (
                        <div className="source-share-row" key={`${source.mmsi || "unknown"}-${index}`}>
                          <span>{source.vessel_name || (source.mmsi ? `MMSI ${source.mmsi}` : "Unknown")}</span>
                          <b>{source.relative_likelihood_percent ?? 0}%</b>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div className="incident-report-section">
                    <h4>Spill Forecast</h4>
                    <div className="forecast-timeline">
                      {(incidentReport.spill_forecast?.timeline_hours || [0]).map((hours: number, index: number) => (
                        <React.Fragment key={`${hours}-${index}`}>
                          {index > 0 && <span>→</span>}
                          <b>{hours === 0 ? "NOW" : `+${Number(hours)}h`}</b>
                        </React.Fragment>
                      ))}
                    </div>
                  </div>

                  <div className="incident-report-section">
                    <h4>Recommended Actions</h4>
                    {(incidentReport.recommended_actions || []).length > 0 ? (
                      <ol className="report-action-list">
                        {incidentReport.recommended_actions.map((item: any) => (
                          <li key={item.order}>{item.action}</li>
                        ))}
                      </ol>
                    ) : <div className="idp-empty-state">Actions are still being calculated.</div>}
                  </div>

                  <small className="report-provenance">Computed from SAR, spatial-risk, AIS attribution, and drift-model records. Missing evidence is not fabricated.</small>
                </>
              ) : (
                <div className="idp-empty-state">Incident analysis is still being generated.</div>
            )}
          </div>
        </div>

        <div className="idp-main-view">
          {sar_tasking ? (
            <div className="idp-card full-height">
              <div className="sar-header-flex">
                <h3>SAR Processing Pipeline</h3>
                <span className="idp-id-badge">
                  {isFailed ? "FAILED" : isFulfilled ? "FULFILLED" : "PENDING"}
                  {" | SCENE: "}
                  {sar_tasking.scene_id || "—"}
                </span>
              </div>

              {/* Why this vessel was tasked */}
              {reasonFactors.length > 0 && (
                <div className="tasking-reason" style={{ marginBottom: "12px" }}>
                  <div className="reason-title">Why SAR was tasked</div>
                  <ul>
                    {reasonFactors.map((f: any, i: number) => (
                      <li key={i}>
                        <b>{String(f.factor || "").replace(/_/g, " ")}</b>
                        {f.contribution != null && <span className="reason-weight"> · contribution {Number(f.contribution).toFixed(1)}</span>}
                        {f.description && <p>{String(f.description)}</p>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {(sar_tasking.requested_at || stageText) && (
                <div style={{ fontSize: "11px", opacity: 0.75, marginBottom: "10px" }}>
                  {stageText}
                  {sar_tasking.requested_at && <> · requested {new Date(sar_tasking.requested_at).toLocaleTimeString("en-US", { hour12: false, timeZone: "UTC" })} UTC</>}
                  {sar_tasking.completed_at && <> · completed {new Date(sar_tasking.completed_at).toLocaleTimeString("en-US", { hour12: false, timeZone: "UTC" })} UTC</>}
                </div>
              )}

              <div className="steps-container" style={{ margin: "0 0 12px" }}>
                {SAR_STEPS.map((step, index) => {
                  const isCompleted = index < currentIdx;
                  const isActive = !isFulfilled && !isFailed && index === currentIdx && !(currentIdx === SAR_STEPS.length - 1 && !isFulfilled);
                  return (
                    <div key={step.id} className={`step-item ${isCompleted ? "completed" : (isActive || (isFulfilled && index === SAR_STEPS.length - 1)) ? "active" : "pending"}`}>
                      <div className="step-circle"></div>
                      <div className="step-copy">
                        <span className="step-label">{step.label}</span>
                        <small className="step-detail">{step.detail}</small>
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="idp-sar-grid">
                <div className="sar-artifact">
                  <div className="sar-label">1. RAW SAR IMAGE</div>
                  <div className="sar-img-wrapper">
                    {images.raw ? (
                      <SARArtifactPreview url={`${images.raw}?cb=${cacheBuster}`} alt="Raw SAR" filename={SAR_ARTIFACT_FILENAMES.raw} />
                    ) : <span className="sar-placeholder">{isFailed ? "UNAVAILABLE" : isFulfilled ? "NOT RETAINED" : "AWAITING ACQUISITION"}</span>}
                  </div>
                </div>

                <div className="sar-artifact">
                  <div className="sar-label">2. DESPECKLED FILTER</div>
                  <div className="sar-img-wrapper">
                    {images.filtered ? (
                      <SARArtifactPreview url={`${images.filtered}?cb=${cacheBuster}`} alt="Despeckled" filename={SAR_ARTIFACT_FILENAMES.filtered} />
                    ) : <span className="sar-placeholder">{isFailed ? "UNAVAILABLE" : `STAGE ${Math.min(currentIdx + 1, 4)} OF 7`}</span>}
                  </div>
                </div>

                <div className="sar-artifact">
                  <div className="sar-label">3. CFAR OBJECT DETECTION</div>
                  <div className="sar-img-wrapper">
                    {images.cfar ? (
                      <SARArtifactPreview url={`${images.cfar}?cb=${cacheBuster}`} alt="CFAR Mask" filename={SAR_ARTIFACT_FILENAMES.cfar} />
                    ) : <span className="sar-placeholder">{isFailed ? "UNAVAILABLE" : `STAGE ${Math.min(Math.max(currentIdx - 2, 1), 4)} OF 7`}</span>}
                  </div>
                </div>

                <div className="sar-artifact">
                  <div className="sar-label">4. CLEANED POLYGON</div>
                  <div className="sar-img-wrapper">
                    {images.final ? (
                      <SARArtifactPreview url={`${images.final}?cb=${cacheBuster}`} alt="Polygon" filename={SAR_ARTIFACT_FILENAMES.final} />
                    ) : <span className="sar-placeholder">{isFailed ? "UNAVAILABLE" : `STAGE ${Math.min(Math.max(currentIdx - 3, 1), 4)} OF 7`}</span>}
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
