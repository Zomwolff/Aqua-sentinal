/**
 * Pure logic for the spill-candidate map layer (Step 8).
 *
 * Kept DOM/framework-free so candidate merging, confidence colour-mapping,
 * synthetic styling and popup content can be unit-tested without a map.
 */

export const CONFIDENCE_MIN = 0.5;
export const CONFIDENCE_MAX = 1.0;
export const COLOR_YELLOW = "rgb(255, 216, 61)";
export const COLOR_RED = "rgb(225, 29, 46)";

export const SPILL_STREAMS = new Set([
  "spill.candidates.filtered",
  "incident.fused",
]);

/** Clamp confidence to [0.5, 1.0]; returns fallback for missing/invalid. */
export function clampConfidence(value, fallback = null) {
  if (typeof value !== "number" || Number.isNaN(value)) return fallback;
  return Math.min(CONFIDENCE_MAX, Math.max(CONFIDENCE_MIN, value));
}

/** Linear yellow->red colour over [0.5, 1.0]; fallback colour when missing. */
export function confidenceColor(value, fallback = COLOR_YELLOW) {
  const c = clampConfidence(value);
  if (c === null) return fallback;
  const t = (c - CONFIDENCE_MIN) / (CONFIDENCE_MAX - CONFIDENCE_MIN);
  const r = Math.round(0xff + (0xe1 - 0xff) * t);
  const g = Math.round(0xd8 + (0x1d - 0xd8) * t);
  const b = Math.round(0x3d + (0x2e - 0x3d) * t);
  return `rgb(${r}, ${g}, ${b})`;
}

export function isSynthetic(value) {
  if (value === undefined || value === null) return false;
  if (typeof value === "boolean") return value;
  return ["1", "true", "yes", "on"].includes(String(value).trim().toLowerCase());
}

/**
 * Redis stream values arrive as strings (publish_to_stream JSON-encodes
 * scalars, None -> ""). Coerce numeric scalar fields back to numbers so
 * confidence colour-mapping and area display are correct. Non-numeric /
 * empty values return null (treated as missing).
 */
function toNumber(value) {
  if (typeof value === "number") return Number.isNaN(value) ? null : value;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed === "") return null;
    const n = Number(trimmed);
    return Number.isNaN(n) ? null : n;
  }
  return null;
}

/** Coerce a correlated_vessel_id that may arrive as a JSON string or "". */
function toCorrelatedVesselId(value) {
  if (value === null || value === undefined) return null;
  if (value === "") return null;
  if (typeof value === "number") return value;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed === "") return null;
    // Numeric id (e.g. "42") -> number; otherwise keep the raw string.
    return /^-?\d+(\.\d+)?$/.test(trimmed) ? Number(trimmed) : trimmed;
  }
  return value;
}

const NUMERIC_FIELDS = new Set(["confidence", "area_m2"]);

const MERGEABLE_FIELDS = [
  "scene_id",
  "acquisition_time",
  "classification_label",
  "confidence",
  "area_m2",
  "texture_features",
  "is_synthetic",
  "correlated_vessel_id",
  "orbit",
  "polarization",
  "resolution",
];

/**
 * Merge one WS event into candidate state keyed by candidate_id.
 * Incident.fused enriches the existing candidate (same stable identity);
 * candidate geometry, once set, is never replaced.
 */
export function mergeSpillEvent(state, event) {
  const data = event?.data ?? event;
  if (!data || data.candidate_id === undefined || data.candidate_id === null) {
    return state;
  }
  const id = String(data.candidate_id);
  const existing = state[id] ?? { candidate_id: id };
  const merged = { ...existing };

  for (const key of MERGEABLE_FIELDS) {
    if (data[key] !== undefined) {
      if (NUMERIC_FIELDS.has(key)) merged[key] = toNumber(data[key]);
      else if (key === "correlated_vessel_id") merged[key] = toCorrelatedVesselId(data[key]);
      else merged[key] = data[key];
    }
  }
  // Explicit (empty/"null") correlated vessel from incident.fused is meaningful.
  if (event?.stream === "incident.fused" && "correlated_vessel_id" in data) {
    merged.correlated_vessel_id = toCorrelatedVesselId(data.correlated_vessel_id);
  }
  if (data.geometry) merged.geometry = data.geometry;
  else if (existing.geometry) merged.geometry = existing.geometry;

  return { ...state, [id]: merged };
}

/** State for the layer's geometry/async bookkeeping. */
export function initialLayerState() {
  return {
    candidates: {},
    geometryCache: {},
    pending: {},
    geometryErrors: {},
  };
}

/** True when an event is a spill-layer event we render. */
export function isSpillEvent(event) {
  return event && SPILL_STREAMS.has(event.stream);
}

export function candidateFromEvent(event, geometry = null) {
  const data = event?.data ?? event;
  return {
    candidate_id: String(data.candidate_id),
    confidence: data.confidence,
    classification_label: data.classification_label,
    is_synthetic: isSynthetic(data.is_synthetic),
    geometry: geometry,
  };
}

/** Leaflet path style. Synthetic candidates always get a dashed border. */
export function candidateStyle(candidate) {
  const synthetic = isSynthetic(candidate?.is_synthetic);
  const color = confidenceColor(candidate?.confidence, COLOR_YELLOW);
  const style = {
    color,
    weight: synthetic ? 3 : 2,
    fillColor: color,
    fillOpacity: synthetic ? 0.25 : 0.45,
  };
  if (synthetic) style.dashArray = "6, 6";
  return style;
}

function formatTexture(texture) {
  if (!texture) return null;
  if (typeof texture === "string") {
    try {
      texture = JSON.parse(texture);
    } catch {
      return texture;
    }
  }
  return JSON.stringify(texture, null, 2);
}

/**
 * Structured popup content. Returns { synthetic, lines, texture, correlated }.
 * Never contains "confirmed" / ground-truth terminology.
 */
export function buildPopupData(candidate) {
  const c = candidate ?? {};
  const synthetic = isSynthetic(c.is_synthetic);
  const correlated =
    c.correlated_vessel_id === null || c.correlated_vessel_id === undefined
      ? "No correlated vessel"
      : String(c.correlated_vessel_id);
  const conf =
    typeof c.confidence === "number" ? c.confidence.toFixed(3) : "n/a";
  const area = typeof c.area_m2 === "number" ? `${c.area_m2.toFixed(1)} m²` : "n/a";

  const lines = [
    { label: "Classification", value: c.classification_label ?? "n/a" },
    { label: "Confidence", value: conf },
    { label: "Area", value: area },
    { label: "Acquisition time", value: c.acquisition_time ?? "n/a" },
    { label: "Correlated vessel", value: correlated },
  ];
  return {
    synthetic,
    header: synthetic ? "SYNTHETIC DEMO" : "Spill candidate",
    lines,
    texture: formatTexture(c.texture_features),
  };
}

/** HTML for the Leaflet popup (rendered via layer.bindPopup). */
export function renderPopupHtml(candidate) {
  const data = buildPopupData(candidate);
  const rows = data.lines
    .map(
      (l) =>
        `<div class="popup-row"><span class="popup-label">${l.label}:</span> <span class="popup-value">${l.value}</span></div>`,
    )
    .join("");
  const texture =
    data.texture !== null
      ? `<pre class="popup-texture">${data.texture}</pre>`
      : "";
  const banner = data.synthetic
    ? '<div class="popup-synthetic">SYNTHETIC DEMO</div>'
    : "";
  return `<div class="spill-popup">${banner}<div class="popup-header">${data.header}</div>${rows}${texture}</div>`;
}