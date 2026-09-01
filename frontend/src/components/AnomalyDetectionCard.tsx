import "./AnomalyDetectionCard.css";

type Evidence = Record<string, unknown>;

interface AnomalyRecord {
  id?: string | number;
  anomaly_type?: string;
  severity?: string;
  window_start?: string;
  detected_at?: string;
  timestamp?: string;
  evidence?: Evidence | string | null;
  source?: string | null;
  confidence_score?: number | null;
  included_in_risk?: boolean | null;
  contributed_to_risk?: boolean | null;
  risk_contribution?: number | null;
  weather_suppressed?: boolean | null;
  weather_adjustment?: number | null;
}

interface Props {
  anomalies?: AnomalyRecord[] | null;
  risk?: {
    contributing_factors?: Array<{
      factor?: string;
      contribution?: number;
      description?: string;
    }>;
  } | null;
}

const LABELS: Record<string, string> = {
  sudden_stop: "Sudden Stop",
  erratic_course: "Erratic Course",
  speed_anomaly: "Speed Anomaly",
  loitering_anomaly: "Loitering",
  route_deviation: "Route Deviation",
  ais_gap: "AIS Gap",
  cog_heading_divergence: "COG vs Heading Divergence",
  erratic_turn: "Erratic Turn Behaviour",
  draught_drop: "Draught Change",
};

const EVIDENCE_LABELS: Record<string, string> = {
  speed_before_kn: "Speed before",
  speed_after_kn: "Speed after",
  avg_speed_kn: "Average speed",
  max_speed_kn: "Maximum speed",
  type_max_kn: "Expected type maximum",
  speed_drop_kn: "Speed drop",
  course_variance: "Course variance",
  max_rate_of_turn_deg_min: "Maximum rate of turn",
  turn_reversal_count: "Turn reversals",
  mean_divergence_deg: "Mean COG/heading divergence",
  max_divergence_deg: "Maximum COG/heading divergence",
  repeated_observations: "Repeated observations",
  loitering_score: "Loitering score",
  gap_minutes: "AIS signal gap",
  gap_threshold_minutes: "AIS gap threshold",
  expected_bearing_deg: "Expected bearing",
  actual_course_deg: "Actual course",
  deviation_deg: "Route deviation",
  draught_change_m: "Draught change",
  confidence_score: "Confidence",
  rule: "Detection rule",
};

function titleCase(value: string): string {
  return value
    .replace(/^statistical_/, "Statistical ")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function anomalyLabel(type = "unknown_anomaly"): string {
  return LABELS[type] || titleCase(type);
}

function parseEvidence(value: AnomalyRecord["evidence"]): Evidence {
  if (!value) return {};
  if (typeof value === "object" && !Array.isArray(value)) return value;
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : { detail: value };
    } catch {
      return { detail: value };
    }
  }
  return {};
}

function number(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) return Number(value);
  return null;
}

function formatValue(key: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return "Unavailable";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.map(String).join(", ");
  if (typeof value === "object") return JSON.stringify(value);

  const numeric = number(value);
  if (numeric !== null) {
    const formatted = Number.isInteger(numeric) ? String(numeric) : numeric.toFixed(2);
    if (key.endsWith("_kn")) return `${formatted} kn`;
    if (key.endsWith("_deg_min")) return `${formatted}°/min`;
    if (key.endsWith("_deg")) return `${formatted}°`;
    if (key.endsWith("_minutes")) return `${formatted} min`;
    if (key.endsWith("_m")) return `${formatted} m`;
    if (key.includes("confidence") && numeric >= 0 && numeric <= 1) return `${Math.round(numeric * 100)}%`;
    return formatted;
  }
  return String(value).replace(/_/g, " ");
}

function firstNumber(evidence: Evidence, keys: string[]): number | null {
  for (const key of keys) {
    const value = number(evidence[key]);
    if (value !== null) return value;
  }
  return null;
}

function explanation(anomaly: AnomalyRecord, evidence: Evidence): string {
  const type = anomaly.anomaly_type || "";
  const before = firstNumber(evidence, ["speed_before_kn", "previous_speed_kn", "max_speed_kn"]);
  const after = firstNumber(evidence, ["speed_after_kn", "current_speed_kn", "avg_speed_kn"]);
  const turnRate = firstNumber(evidence, ["max_rate_of_turn_deg_min", "heading_change_rate"]);
  const reversals = firstNumber(evidence, ["turn_reversal_count"]);
  const gap = firstNumber(evidence, ["gap_minutes"]);
  const deviation = firstNumber(evidence, ["deviation_deg"]);
  const loitering = firstNumber(evidence, ["loitering_score"]);
  const divergence = firstNumber(evidence, ["max_divergence_deg", "mean_divergence_deg"]);
  const draught = firstNumber(evidence, ["draught_change_m"]);

  if (type === "sudden_stop" && before !== null && after !== null) return `Vessel speed dropped from ${before.toFixed(1)} kn to ${after.toFixed(1)} kn.`;
  if ((type === "erratic_course" || type === "erratic_turn") && turnRate !== null) return `Rapid course changes were detected, reaching ${turnRate.toFixed(1)}° per minute.`;
  if ((type === "erratic_course" || type === "erratic_turn") && reversals !== null) return `${reversals} turn reversals were detected in the analysis window.`;
  if (type === "ais_gap" && gap !== null) return `The AIS signal was unavailable for ${gap.toFixed(1)} minutes.`;
  if (type === "route_deviation" && deviation !== null) return `The vessel deviated ${deviation.toFixed(1)}° from its expected route bearing.`;
  if (type === "loitering_anomaly" && loitering !== null) return `Sustained low-speed behaviour produced a loitering score of ${loitering.toFixed(2)}.`;
  if (type === "cog_heading_divergence" && divergence !== null) return `Course over ground and reported heading differed by up to ${divergence.toFixed(1)}°.`;
  if (type === "draught_drop" && draught !== null) return `A measured draught change of ${draught.toFixed(2)} m was detected.`;
  if (type === "speed_anomaly" && after !== null) return `The observed speed of ${after.toFixed(1)} kn was outside the expected operating range.`;
  if (typeof evidence.rule === "string") return `Triggered by the ${String(evidence.rule).replace(/_/g, " ")} rule.`;
  return "The monitoring system detected behaviour outside the expected vessel pattern.";
}

function formatTimestamp(anomaly: AnomalyRecord): string {
  const raw = anomaly.detected_at || anomaly.timestamp || anomaly.window_start;
  if (!raw) return "Timestamp unavailable";
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return String(raw);
  return `${parsed.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" })}`;
}

function riskStatus(anomaly: AnomalyRecord): { label: string; state: string } {
  if (typeof anomaly.risk_contribution === "number") {
    return { label: `${anomaly.risk_contribution.toFixed(1)} AIS risk points`, state: "included" };
  }
  const included = anomaly.included_in_risk ?? anomaly.contributed_to_risk;
  if (included === true) return { label: "Included in AIS risk calculation", state: "included" };
  if (included === false) return { label: "Not included in AIS risk calculation", state: "excluded" };
  return { label: "Per-anomaly risk effect not provided by backend", state: "unknown" };
}

export function AnomalyDetectionCard({ anomalies, risk }: Props) {
  const records = Array.isArray(anomalies) ? anomalies : [];
  const aggregateFactor = risk?.contributing_factors?.find((factor) => factor.factor === "anomaly");

  return (
    <section className="idp-card anomaly-detection-card" aria-labelledby="anomaly-detection-title">
      <div className="anomaly-card-heading">
        <div>
          <h3 id="anomaly-detection-title">Anomaly Detection</h3>
          <p>The system continuously monitors AIS behaviour for abnormal vessel patterns.</p>
        </div>
        <span className="anomaly-monitoring-status"><i /> Monitoring active</span>
      </div>

      {records.length === 0 ? (
        <div className="anomaly-empty-state">
          <span className="anomaly-empty-icon">✓</span>
          <div>
            <strong>No anomalies detected</strong>
            <p>No abnormal AIS behaviour was returned for this vessel in the backend monitoring window.</p>
          </div>
        </div>
      ) : (
        <>
          <div className="anomaly-count-banner">
            <span aria-hidden="true">⚠</span>
            <strong>{records.length} {records.length === 1 ? "anomaly" : "anomalies"} detected</strong>
          </div>

          <div className="anomaly-list">
            {records.map((anomaly, index) => {
              const severity = String(anomaly.severity || "UNKNOWN").toUpperCase();
              const evidence = parseEvidence(anomaly.evidence);
              const entries = Object.entries(evidence).filter(([, value]) => value !== null && value !== undefined && value !== "");
              const riskEffect = riskStatus(anomaly);
              return (
                <article className={`anomaly-item severity-${severity.toLowerCase()}`} key={anomaly.id ?? `${anomaly.anomaly_type}-${anomaly.window_start}-${index}`}>
                  <div className="anomaly-item-topline">
                    <span className={`anomaly-severity severity-${severity.toLowerCase()}`}>{severity}</span>
                    <time>{formatTimestamp(anomaly)}</time>
                  </div>
                  <h4>{anomalyLabel(anomaly.anomaly_type)}</h4>
                  <p className="anomaly-explanation">{explanation(anomaly, evidence)}</p>

                  <div className={`anomaly-risk-link ${riskEffect.state}`}>
                    <span>AIS risk relationship</span>
                    <strong>{riskEffect.label}</strong>
                  </div>

                  {(anomaly.source || anomaly.confidence_score != null || anomaly.weather_suppressed != null || anomaly.weather_adjustment != null) && (
                    <div className="anomaly-metadata">
                      {anomaly.source && <span>Source: <b>{titleCase(anomaly.source)}</b></span>}
                      {anomaly.confidence_score != null && <span>Confidence: <b>{formatValue("confidence_score", anomaly.confidence_score)}</b></span>}
                      {anomaly.weather_suppressed != null && <span>Weather suppression: <b>{anomaly.weather_suppressed ? "Applied" : "Not applied"}</b></span>}
                      {anomaly.weather_adjustment != null && <span>Weather adjustment: <b>{formatValue("weather_adjustment", anomaly.weather_adjustment)}</b></span>}
                    </div>
                  )}

                  <details className="anomaly-evidence">
                    <summary>View supporting evidence <span>{entries.length}</span></summary>
                    {entries.length > 0 ? (
                      <dl>
                        {entries.map(([key, value]) => (
                          <div key={key}>
                            <dt>{EVIDENCE_LABELS[key] || titleCase(key)}</dt>
                            <dd>{formatValue(key, value)}</dd>
                          </div>
                        ))}
                      </dl>
                    ) : (
                      <p>No supporting evidence values were provided by the backend.</p>
                    )}
                  </details>
                </article>
              );
            })}
          </div>
        </>
      )}

      <div className="anomaly-risk-summary">
        <div>
          <span>Aggregate anomaly effect on AIS risk</span>
          <strong>{aggregateFactor?.contribution != null ? `${Number(aggregateFactor.contribution).toFixed(1)} points` : "Not provided"}</strong>
        </div>
        <p>
          {aggregateFactor?.description
            ? String(aggregateFactor.description).replace(/_/g, " ")
            : "The backend has not supplied an aggregate anomaly contribution for this vessel."}
        </p>
        <small>AIS anomalies describe vessel behaviour. They do not confirm the presence of an oil spill.</small>
      </div>
    </section>
  );
}
