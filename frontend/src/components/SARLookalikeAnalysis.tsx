import { useMemo, useState, type ReactNode } from "react";
import "./SARLookalikeAnalysis.css";

type Props = {
  incidentDetail: any;
  onShowCandidate?: () => void;
};

type CandidateState = "ship-shadow" | "calm-water" | "possible-slick" | "unavailable";

const first = (...values: any[]) => values.find((value) => value !== undefined && value !== null && value !== "");
const num = (...values: any[]): number | null => {
  const value = first(...values);
  return value === undefined || !Number.isFinite(Number(value)) ? null : Number(value);
};
const display = (value: any, suffix = "", digits = 2) => {
  if (value === undefined || value === null || value === "" || !Number.isFinite(Number(value))) return "Unavailable";
  return `${Number(value).toFixed(digits)}${suffix}`;
};
const percent = (value: any) => {
  const parsed = num(value);
  if (parsed === null) return "Unavailable";
  return `${(parsed <= 1 ? parsed * 100 : parsed).toFixed(0)}%`;
};
const bool = (value: any) => value === true ? "Yes" : value === false ? "No" : "Unavailable";

function candidateState(label: unknown): CandidateState {
  const normalized = String(label || "").toLowerCase().replace(/_/g, "-");
  if (normalized.includes("ship-shadow")) return "ship-shadow";
  if (normalized.includes("calm-water")) return "calm-water";
  if (normalized.includes("possible-slick") || normalized.includes("possible-oil-spill") || normalized === "low-confidence") return "possible-slick";
  return "unavailable";
}

const stateCopy: Record<CandidateState, { label: string; summary: string }> = {
  "ship-shadow": { label: "LIKELY SHIP SHADOW", summary: "Rejected because its geometry and nearby bright-target evidence are consistent with a radar shadow." },
  "calm-water": { label: "LIKELY CALM WATER", summary: "Rejected because its diffuse boundary and uniform dark texture are consistent with calm water." },
  "possible-slick": { label: "POSSIBLE SLICK", summary: "This candidate survived the lookalike filters and received a heuristic confidence score." },
  unavailable: { label: "ANALYSIS UNAVAILABLE", summary: "The incident response does not currently include candidate-level lookalike evidence." },
};

function Metric({ label, value, tip }: { label: string; value: string; tip: string }) {
  return <div className="sla-metric" title={tip}><span>{label}<i>?</i></span><b>{value}</b></div>;
}

function EvidenceSection({ title, subtitle, children, open = false }: { title: string; subtitle: string; children: ReactNode; open?: boolean }) {
  return <details className="sla-details" open={open}><summary><span><b>{title}</b><small>{subtitle}</small></span><i>⌄</i></summary><div className="sla-detail-body">{children}</div></details>;
}

export function SARLookalikeAnalysis({ incidentDetail, onShowCandidate }: Props) {
  const [showFlow, setShowFlow] = useState(false);
  const analysis = useMemo(() => {
    const incident = incidentDetail?.incident || {};
    const root = first(
      incidentDetail?.lookalike_analysis,
      incidentDetail?.sar_lookalike_analysis,
      incidentDetail?.candidate,
      incidentDetail?.spill_candidate,
      incident?.lookalike_analysis,
    ) || {};
    const shape = first(root.shape, root.shape_descriptors, root.descriptors) || {};
    const texture = first(root.texture, root.texture_features) || {};
    const edge = first(root.edge, root.boundary, root.edge_analysis) || {};
    const context = first(root.context, root.context_analysis) || {};
    const scores = first(root.score_components, root.confidence_breakdown, root.scores) || {};
    const label = first(root.classification_label, root.classification, root.status);
    const state = candidateState(label);
    return { incident, root, shape, texture, edge, context, scores, label, state };
  }, [incidentDetail]);

  const { root, shape, texture, edge, context, scores, state } = analysis;
  const modelVersion = first(root.model_version, texture.model_version, incidentDetail?.fusion_metadata?.model_version);
  const isOnnx = String(modelVersion || "").toLowerCase().includes("onnx");
  const confidence = first(root.confidence, root.final_confidence, root.candidate_confidence);
  const rejectionReason = first(
    root.rejection_reason,
    root.reason,
    root.explanation,
    isOnnx && state === "possible-slick"
      ? "The segmentation probability exceeded the configured detection threshold."
      : stateCopy[state].summary,
  );
  const brightDistance = first(root.distance_to_bright_target_m, root.ship_shadow?.distance_to_bright_target_m, shape.distance_to_bright_target_m);
  const adjacentBright = first(root.adjacent_bright_target, root.ship_shadow?.adjacent_bright_target, brightDistance !== undefined ? Number(brightDistance) <= 150 : undefined);
  const boundaryLabel = first(edge.classification, edge.boundary_type, root.boundary_type);
  return <section className="idp-card sla-card" aria-labelledby="sar-lookalike-heading">
    <div className="sla-heading">
      <div><small>SAR CANDIDATE EXPLAINABILITY</small><h3 id="sar-lookalike-heading">SAR Lookalike Analysis</h3></div>
      {onShowCandidate && <button className="sla-image-link" onClick={onShowCandidate}>◎ Show candidate image</button>}
    </div>

    <div className={`sla-verdict ${state}`}>
      <div><span>Candidate status</span><strong>{stateCopy[state].label}</strong></div>
      <div><span>{isOnnx ? "Model probability" : "Candidate confidence"}</span><strong>{percent(confidence)}</strong></div>
      <p>{String(rejectionReason)}</p>
      <small>{modelVersion ? `Method: ${modelVersion}` : "Method version unavailable"}</small>
    </div>

    {state === "unavailable" && <div className="sla-data-notice"><b>Candidate evidence was not returned by the API.</b><span>This panel is integration-ready and will populate when the incident response includes lookalike analysis. Values are never estimated in the browser.</span></div>}

    <div className="sla-sections">
      <EvidenceSection title="Ship shadow check" subtitle="Shape and proximity to a bright SAR vessel target" open={state === "ship-shadow"}>
        <div className="sla-metric-grid"><Metric label="Elongation" value={display(first(shape.elongation, root.elongation), ":1", 1)} tip="Ratio between the candidate's long and short axes."/><Metric label="Eccentricity" value={display(first(shape.eccentricity, root.eccentricity))} tip="How stretched the region is; values nearer 1 are more elongated."/><Metric label="Bright-target distance" value={display(brightDistance, " m", 1)} tip="Distance from the dark region to a bright radar return that may represent a vessel."/><Metric label="Adjacent bright target" value={bool(adjacentBright)} tip="Whether a bright SAR target was found beside the candidate."/><Metric label="Boundary" value={String(boundaryLabel || "Unavailable")} tip="Sharp boundaries can support a distinct surface anomaly or a radar shadow, depending on context."/></div>
        <p className="sla-interpretation"><b>Result:</b> {state === "ship-shadow" ? "Likely ship shadow" : state === "unavailable" ? "Unavailable" : "Not rejected as a ship shadow"}. {state === "ship-shadow" ? String(rejectionReason) : "The candidate status did not identify the ship-shadow heuristic as the rejection cause."}</p>
      </EvidenceSection>

      <EvidenceSection title="Calm-water check" subtitle="Backscatter, texture, solidity and boundary diffusion" open={state === "calm-water"}>
        <div className="sla-metric-grid"><Metric label="Mean backscatter" value={display(first(texture.mean_backscatter, root.mean_backscatter_db), " dB", 1)} tip="Average radar return inside the candidate."/><Metric label="Boundary gradient" value={display(first(edge.gradient, edge.boundary_gradient, root.boundary_gradient))} tip="Strength of the transition between the region and surrounding water."/><Metric label="Solidity" value={display(first(shape.solidity, root.solidity))} tip="How completely the candidate fills its convex outline."/><Metric label="Texture" value={String(first(texture.classification, texture.texture_label, root.texture_label) || "Unavailable")} tip="A plain-language description of radar variation within the region."/><Metric label="Region shape" value={String(first(shape.shape_label, root.shape_label) || "Unavailable")} tip="Backend description of the candidate geometry."/></div>
        <p className="sla-interpretation"><b>Result:</b> {state === "calm-water" ? "Likely calm water" : state === "unavailable" ? "Unavailable" : "Not rejected as calm water"}. {state === "calm-water" ? String(rejectionReason) : "The candidate status did not identify calm water as the rejection cause."}</p>
      </EvidenceSection>

      <EvidenceSection title="Shape analysis" subtitle="Geometric evidence used by the rejection filters">
        <div className="sla-metric-grid"><Metric label="Area" value={display(first(shape.area_px, root.area_px), " px", 0)} tip="Number of image pixels covered by the candidate."/><Metric label="Eccentricity" value={display(first(shape.eccentricity, root.eccentricity))} tip="How stretched the candidate is; 0 is circular and values nearer 1 are elongated."/><Metric label="Elongation" value={display(first(shape.elongation, root.elongation), ":1", 1)} tip="Long-axis to short-axis ratio."/><Metric label="Solidity" value={display(first(shape.solidity, root.solidity))} tip="Candidate area divided by its convex-hull area."/><Metric label="Perimeter / area" value={display(first(shape.perimeter_area_ratio, shape.perimeter_to_area_ratio, root.perimeter_area_ratio))} tip="Boundary length relative to region size; higher values can indicate a thin or irregular region."/></div>
        <p className="sla-interpretation">Shape characteristics help distinguish natural water patterns and ship-related shadows from slick-like regions.</p>
      </EvidenceSection>

      <EvidenceSection title="Edge / boundary analysis" subtitle="Transition between the candidate and surrounding water">
        <div className="sla-metric-grid"><Metric label="Inside region" value={display(first(edge.inside_backscatter_db, edge.inside_mean_db), " dB", 1)} tip="Mean radar backscatter within the candidate."/><Metric label="Outside region" value={display(first(edge.outside_backscatter_db, edge.outside_mean_db), " dB", 1)} tip="Mean backscatter immediately outside the candidate."/><Metric label="Edge gradient" value={display(first(edge.gradient, edge.boundary_gradient))} tip="Magnitude of the backscatter transition across the boundary."/><Metric label="Boundary" value={String(boundaryLabel || "Unavailable")} tip="Backend classification of the transition as sharp or diffuse."/></div>
        <p className="sla-interpretation">A strong backscatter difference can be consistent with a distinct surface anomaly. It is one heuristic signal, not proof of oil.</p>
      </EvidenceSection>

      <EvidenceSection title="SAR texture analysis" subtitle={state === "possible-slick" ? "Computed for candidates that survive rejection" : "Only expected for surviving candidates"}>
        <div className="sla-metric-grid"><Metric label="GLCM energy" value={display(texture.energy)} tip="Measures texture uniformity; higher energy indicates a more regular pattern."/><Metric label="GLCM contrast" value={display(texture.contrast)} tip="Measures local variation in radar backscatter."/><Metric label="GLCM homogeneity" value={display(texture.homogeneity)} tip="Measures how similar neighboring radar values are."/><Metric label="Texture score" value={percent(first(texture.texture_score, scores.texture, scores.texture_score))} tip="Texture contribution returned by the scoring model."/></div>
        <p className="sla-interpretation">Texture analysis evaluates how uniform or variable the radar backscatter pattern is inside the candidate.</p>
      </EvidenceSection>

      <EvidenceSection title="Context analysis" subtitle="Vessel, spatial and environmental evidence">
        <div className="sla-metric-grid"><Metric label="Context score" value={percent(first(context.score, scores.context, scores.context_score))} tip="Measured support from saved AIS positions within 20 km and six hours."/><Metric label="Nearby vessel" value={context.nearby_vessel_found === false ? "None within window" : String(first(context.nearby_vessel_name, context.nearby_vessel_mmsi, root.nearby_vessel) || "Unavailable")} tip="Vessel identified near the SAR candidate, when available."/><Metric label="Vessel distance" value={display(first(context.vessel_distance_m, root.nearby_vessel_distance_m), " m", 1)} tip="Spatial distance between the candidate and contextual vessel evidence."/><Metric label="Time gap" value={display(context.time_gap_hours, " h", 2)} tip="Time between the saved AIS position and satellite acquisition."/><Metric label="Context source" value={String(context.source || "Unavailable")} tip="Data source used by the backend context calculation."/></div>
      </EvidenceSection>
    </div>

    <button className="sla-flow-toggle" onClick={() => setShowFlow((visible) => !visible)}>{showFlow ? "Hide" : "Show"} lookalike decision flow <span>→</span></button>
    {showFlow && <div className="sla-flow" aria-label="SAR lookalike decision flow"><span>SAR dark region</span><i>↓</i><span>Shape analysis</span><i>↓</i><span className="filter">Lookalike rejection<small>Ship shadow? · Calm water?</small></span><i>↓</i><span>Survives filters</span><i>↓</i><span>Texture + context</span><i>↓</i><span>Confidence score</span><i>↓</i><span>Possible slick</span><i>↓</i><span>Downstream evidence fusion</span></div>}

    <div className="sla-method"><b>Why was this classified this way?</b><p>{isOnnx ? "The SAR ONNX segmentation probability crossed its configured threshold. Shape, texture, boundary, and AIS context are measured supporting evidence and do not overwrite that probability." : `${stateCopy[state].summary} The SAR stage uses heuristic evidence and lookalike rejection.`} “Possible slick” does not mean confirmed oil; final incident assessment requires downstream evidence fusion and operator review.</p><small>Technical values are displayed exactly as supplied by the backend and are not recalculated in the frontend.</small></div>
  </section>;
}
