import { useState } from "react";
import "./SourceAttributionAnalysis.css";

type Props = { attribution?: any[]; incident?: any; onSpotVessel?: (mmsi: string) => void };
type Factor = { key: string; label: string; apiKey: string; defaultWeight: number; why: string; evidenceKeys: string[] };

const FACTORS: Factor[] = [
  { key: "distance", label: "Distance", apiKey: "distance_score", defaultWeight: .25, why: "A vessel closer to the detected spill location receives stronger spatial evidence.", evidenceKeys: ["distance_m", "vessel_distance_m"] },
  { key: "trajectory", label: "Trajectory", apiKey: "trajectory_score", defaultWeight: .25, why: "The AIS track can pass through the spill area even if the latest vessel position is farther away.", evidenceKeys: ["closest_approach_m", "trajectory_distance_m"] },
  { key: "time", label: "Time", apiKey: "time_score", defaultWeight: .20, why: "Vessels observed closer to SAR acquisition time receive stronger temporal evidence.", evidenceKeys: ["hours_from_acquisition", "time_difference_hours"] },
  { key: "behavior", label: "Behavior", apiKey: "behavior_score", defaultWeight: .15, why: "Relevant AIS anomalies add operational context but do not establish responsibility.", evidenceKeys: ["anomaly_summary", "behavior_evidence"] },
  { key: "wind", label: "Wind / drift", apiKey: "wind_score", defaultWeight: .15, why: "Wind and current movement are considered when estimating where the slick may have originated.", evidenceKeys: ["backward_drift_distance_m", "drift_origin_distance_m"] },
];

const value = (...items: any[]) => items.find((item) => item !== undefined && item !== null && item !== "");
const number = (...items: any[]): number | null => { const found = value(...items); return found === undefined || !Number.isFinite(Number(found)) ? null : Number(found); };
const asPercent = (raw: any): number | null => { const parsed = number(raw); return parsed === null ? null : parsed <= 1 ? parsed * 100 : parsed; };
const pct = (raw: any, digits = 0) => { const parsed = asPercent(raw); return parsed === null ? "Unavailable" : `${parsed.toFixed(digits)}%`; };
const classification = (raw: any) => { const score = asPercent(raw); if (score === null) return "INSUFFICIENT DATA"; if (score >= 75) return "PROBABLE SOURCE"; if (score >= 50) return "POSSIBLE SOURCE"; if (score >= 25) return "CORRELATED"; return "INSUFFICIENT EVIDENCE"; };

function factorWeight(attr: any, factor: Factor) {
  const configured = number(attr?.weights?.[factor.key], attr?.[`${factor.key}_weight`]);
  if (configured === null) return { weight: factor.defaultWeight, source: "model default" };
  return { weight: configured > 1 ? configured / 100 : configured, source: "backend" };
}

function evidenceText(attr: any, factor: Factor) {
  const explicit = value(attr?.evidence?.[factor.key], ...factor.evidenceKeys.map((key) => attr?.[key]));
  if (explicit === undefined) return "Detailed measurement unavailable from the current API response.";
  if (typeof explicit === "string") return explicit;
  if (factor.key === "distance" || factor.key === "trajectory" || factor.key === "wind") return `${(Number(explicit) / 1000).toFixed(2)} km`;
  if (factor.key === "time") return `${Math.abs(Number(explicit)).toFixed(1)} hours from SAR acquisition`;
  return String(explicit);
}

function FactorDetail({ attr, factor }: { attr: any; factor: Factor }) {
  const score = asPercent(attr?.[factor.apiKey]);
  const { weight, source } = factorWeight(attr, factor);
  const backendContribution = number(attr?.contributions?.[factor.key], attr?.[`${factor.key}_contribution`]);
  const contribution = backendContribution ?? (score === null ? null : score * weight);
  return <details className="saa-factor"><summary><span><b>{factor.label}</b><small>{pct(attr?.[factor.apiKey])} score · {(weight * 100).toFixed(0)}% weight</small></span><strong>{contribution === null ? "Unavailable" : `${contribution.toFixed(2)} pts`}</strong></summary><div><dl><div><dt>Factor score</dt><dd>{pct(attr?.[factor.apiKey], 1)}</dd></div><div><dt>Model weight</dt><dd>{(weight * 100).toFixed(0)}% <small>({source})</small></dd></div><div><dt>Contribution</dt><dd>{contribution === null ? "Unavailable" : `${contribution.toFixed(2)} points`}</dd></div></dl><section><b>Evidence</b><p>{evidenceText(attr, factor)}</p></section><section><b>Why it matters</b><p>{factor.why}</p></section></div></details>;
}

function Candidate({ attr, rank, incident, onSpotVessel, defaultOpen }: { attr: any; rank: number; incident: any; onSpotVessel?: (mmsi: string) => void; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const final = asPercent(attr?.final_score);
  const label = classification(attr?.final_score);
  const name = attr?.vessel_name || (attr?.mmsi ? `MMSI ${attr.mmsi}` : "Unnamed vessel");
  const supportedReasons = FACTORS.flatMap((factor) => {
    const score = asPercent(attr?.[factor.apiKey]);
    if (score === null || score < 50) return [];
    const copies: Record<string, string> = { distance: "It was geographically close to the estimated source area.", trajectory: "Its AIS trajectory is spatially consistent with the spill area.", time: "It was observed near the SAR acquisition time.", behavior: "Relevant AIS behaviour contributed to the score.", wind: "Wind/current backtracking is consistent with its position." };
    return [copies[factor.key]];
  });

  return <article className={`saa-candidate ${open ? "expanded" : ""}`}>
    <button className="saa-candidate-head" onClick={() => setOpen((current) => !current)} aria-expanded={open}>
      <span className="saa-rank">#{rank}</span><span className="saa-vessel"><b>{name}</b><small>{attr?.mmsi ? `MMSI ${attr.mmsi}` : "MMSI unavailable"} · Model {attr?.model_version || "Unavailable"}</small></span><span className="saa-result"><b>{final === null ? "Unavailable" : `${final.toFixed(1)}%`}</b><small>{label}</small></span><i>⌄</i>
    </button>
    {open && <div className="saa-candidate-body">
      <div className="saa-why"><b>Why this vessel?</b>{supportedReasons.length ? <ul>{supportedReasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : <p>No positive factor explanation is available from the returned scores.</p>}</div>

      <div className="saa-contributions"><div className="saa-subhead"><b>Contribution to final score</b><span>Final score remains the backend value.</span></div>{FACTORS.map((factor) => { const score = asPercent(attr?.[factor.apiKey]); const { weight } = factorWeight(attr, factor); const supplied = number(attr?.contributions?.[factor.key], attr?.[`${factor.key}_contribution`]); const points = supplied ?? (score === null ? null : score * weight); return <div className="saa-bar" key={factor.key}><span>{factor.label}</span><div><i style={{ width: `${points === null ? 0 : Math.min(100, points / factor.defaultWeight)}%` }}/></div><b>{points === null ? "Unavailable" : `${points.toFixed(2)} pts`}</b></div>; })}<div className="saa-total"><span>Backend final score</span><b>{pct(attr?.final_score, 1)}</b></div></div>

      <div className="saa-factors">{FACTORS.map((factor) => <FactorDetail attr={attr} factor={factor} key={factor.key}/>)}</div>

      <details className="saa-calculation"><summary>How was this score calculated?</summary><p>The model combines five independent evidence signals using the displayed weights. Contributions are shown for auditability, while <b>{pct(attr?.final_score, 1)}</b> is the authoritative final value returned by the backend.</p><div>{FACTORS.map((factor) => { const { weight, source } = factorWeight(attr, factor); return <span key={factor.key}><b>{(weight * 100).toFixed(0)}%</b> {factor.label}<small>{source}</small></span>; })}</div><p>Classification: <strong>{label}</strong></p></details>

      <div className="saa-timeline"><div className="saa-subhead"><b>Evidence timeline</b><span>Only timestamps returned by the API are shown.</span></div><div><span><time>{incident?.detected_at ? new Date(incident.detected_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "Unavailable"}</time><b>SAR image acquired</b></span><i>→</i><span><time>{attr?.computed_at ? new Date(attr.computed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "Unavailable"}</time><b>Attribution calculated</b></span></div></div>

      {onSpotVessel && attr?.mmsi && <button className="saa-map" onClick={() => onSpotVessel(String(attr.mmsi))}>◎ Spot vessel and spill on map</button>}
    </div>}
  </article>;
}

export function SourceAttributionAnalysis({ attribution = [], incident, onSpotVessel }: Props) {
  return <section className="idp-card saa-card"><div className="saa-heading"><div><small>SOURCE ATTRIBUTION · CIRCUMSTANTIAL EVIDENCE</small><h3>Probable Source Analysis</h3></div><span>{attribution.length} candidate{attribution.length === 1 ? "" : "s"}</span></div>{attribution.length ? <div className="saa-list">{attribution.map((attr, index) => <Candidate key={`${attr.mmsi || "candidate"}-${index}`} attr={attr} rank={index + 1} incident={incident} onSpotVessel={onSpotVessel} defaultOpen={index === 0}/>)}</div> : <div className="idp-empty-state">No nearby vessels were correlated with this incident.</div>}
    <div className="saa-thresholds"><div><b>Attribution classification</b><span>≥75% Probable Source</span><span>≥50% Possible Source</span><span>≥25% Correlated</span><span>&lt;25% Insufficient Evidence</span></div><p><b>Important:</b> Probable Source does not mean confirmed source. This score measures circumstantial evidence strength and does not establish guilt, responsibility, or legal causation.</p></div>
  </section>;
}
