export const ATTRIBUTION_FACTORS = [
  { key: "distance", label: "Distance", weight: .25, why: "Closer vessels provide stronger spatial evidence." },
  { key: "trajectory", label: "Trajectory", weight: .25, why: "The historical AIS track may intersect the spill vicinity." },
  { key: "time", label: "Time", weight: .20, why: "Observations closer to SAR acquisition provide stronger temporal evidence." },
  { key: "behavior", label: "Behavior", weight: .15, why: "Relevant AIS anomalies add context; they do not establish responsibility." },
  { key: "wind_drift", label: "Wind / drift", weight: .15, why: "Backtracking wind and current can relate an estimated origin to a vessel." },
] as const;

export const numberValue = (...values: any[]): number | null => {
  for (const value of values) {
    if (value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value))) return Number(value);
  }
  return null;
};

export const percentValue = (...values: any[]): number | null => {
  const value = numberValue(...values);
  if (value === null) return null;
  return value <= 1 ? value * 100 : value;
};

export const pct = (value: number | null, digits = 0) => value === null ? "Unavailable" : `${value.toFixed(digits)}%`;
export const metric = (value: number | null, unit: string, digits = 1) => value === null ? "Unavailable" : `${value.toFixed(digits)} ${unit}`;

export function classification(score: number | null) {
  if (score === null) return "INSUFFICIENT DATA";
  if (score >= 75) return "PROBABLE SOURCE";
  if (score >= 50) return "POSSIBLE SOURCE";
  if (score >= 25) return "CORRELATED";
  return "INSUFFICIENT EVIDENCE";
}

export function attributionFactors(attr: any) {
  return ATTRIBUTION_FACTORS.map((factor) => {
    const configured = numberValue(attr?.weights?.[factor.key], attr?.[`${factor.key}_weight`], factor.weight) ?? factor.weight;
    const weight = configured > 1 ? configured / 100 : configured;
    const score = percentValue(attr?.[`${factor.key}_score`], attr?.scores?.[factor.key]);
    const contribution = numberValue(attr?.contributions?.[factor.key], attr?.[`${factor.key}_contribution`]);
    return { ...factor, weight, score, contribution: contribution ?? (score === null ? null : score * weight) };
  });
}

export function normalizeRecommendations(rows: any[] = []) {
  const seen = new Set<string>();
  return rows.flatMap((raw, index) => {
    const title = String(raw.title || raw.action || raw.recommendation || `Response action ${index + 1}`);
    const key = title.toLowerCase().replace(/notify|alert|about|the|\s+/g, "").slice(0, 42);
    if (seen.has(key)) return [];
    seen.add(key);
    return [{
      ...raw,
      title,
      description: raw.description || raw.recommendation || "Operator review required.",
      priority: String(raw.priority || "MEDIUM").toUpperCase(),
      status: String(raw.status || "PENDING").toUpperCase().replace(/_/g, " "),
      evidence: raw.evidence || raw.triggered_by || [],
      synthetic: Boolean(raw.is_synthetic),
    }];
  });
}
