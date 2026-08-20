import { describe, expect, it } from "vitest";

import {
  COLOR_RED,
  COLOR_YELLOW,
  buildPopupData,
  candidateFromEvent,
  candidateStyle,
  clampConfidence,
  confidenceColor,
  isSynthetic,
  isSpillEvent,
  mergeSpillEvent,
  renderPopupHtml,
} from "../spillEvents";

const FILTERED = {
  stream: "spill.candidates.filtered",
  data: {
    candidate_id: "c-1",
    scene_id: "S1",
    confidence: 0.75,
    classification_label: "possible_oil_spill",
    is_synthetic: false,
    acquisition_time: "2024-08-20T09:40:00.000Z",
  },
};

const FUSED = {
  stream: "incident.fused",
  data: {
    candidate_id: "c-1",
    scene_id: "S1",
    confidence: 0.75,
    classification_label: "possible_oil_spill",
    is_synthetic: false,
    acquisition_time: "2024-08-20T09:40:00.000Z",
    correlated_vessel_id: 42,
    correlated_vessel: { mmsi: "123456789", tier: "HIGH" },
  },
};

const GEOMETRY = {
  type: "Polygon",
  coordinates: [[[72.75, 19.15], [72.76, 19.15], [72.76, 19.16], [72.75, 19.16], [72.75, 19.15]]],
};

describe("event -> candidate state", () => {
  it("creates a candidate from a spill.candidates.filtered event", () => {
    const state = mergeSpillEvent({}, FILTERED);
    expect(state["c-1"].candidate_id).toBe("c-1");
    expect(state["c-1"].confidence).toBe(0.75);
  });

  it("incident.fused enriches the existing candidate, not a duplicate", () => {
    let state = mergeSpillEvent({}, { ...FILTERED, data: { ...FILTERED.data, geometry: GEOMETRY } });
    expect(Object.keys(state)).toHaveLength(1);
    state = mergeSpillEvent(state, FUSED);
    expect(Object.keys(state)).toHaveLength(1); // still one candidate
    expect(state["c-1"].correlated_vessel_id).toBe(42);
    expect(state["c-1"].geometry).toEqual(GEOMETRY); // polygon stays stable
  });

  it("repeated filtered events for the same candidate do not duplicate", () => {
    let state = mergeSpillEvent({}, FILTERED);
    state = mergeSpillEvent(state, { ...FILTERED, data: { ...FILTERED.data, confidence: 0.8 } });
    expect(Object.keys(state)).toHaveLength(1);
    expect(state["c-1"].confidence).toBe(0.8);
  });

  it("malformed events do not crash and unknown fields are ignored", () => {
    expect(mergeSpillEvent({}, null)).toEqual({});
    expect(mergeSpillEvent({}, { data: {} })).toEqual({});
    expect(mergeSpillEvent({}, { stream: "unknown", data: { candidate_id: "x" } })).toEqual({
      x: { candidate_id: "x" },
    });
    expect(isSynthetic(undefined)).toBe(false);
    expect(isSynthetic(null)).toBe(false);
    expect(clampConfidence("oops")).toBe(null);
  });

  it("WS string-encoded confidence is coerced to a number (Redis serialises scalars to strings)", () => {
    const event = { stream: "spill.candidates.filtered", data: { candidate_id: "c-1", confidence: "0.73" } };
    const state = mergeSpillEvent({}, event);
    expect(typeof state["c-1"].confidence).toBe("number");
    expect(state["c-1"].confidence).toBe(0.73);
    // A string confidence must colour correctly, not fall back to yellow.
    expect(confidenceColor(state["c-1"].confidence)).not.toBe(COLOR_YELLOW);
    expect(confidenceColor(state["c-1"].confidence)).toBe(confidenceColor(0.73));
  });

  it("incident.fused empty/None correlated_vessel_id becomes null -> 'No correlated vessel'", () => {
    const fused = {
      stream: "incident.fused",
      data: { candidate_id: "c-1", correlated_vessel_id: "" },
    };
    const state = mergeSpillEvent({}, fused);
    expect(state["c-1"].correlated_vessel_id).toBe(null);
    expect(renderPopupHtml(state["c-1"])).toContain("No correlated vessel");
  });

  it("incident.fused numeric-string correlated_vessel_id is coerced to a number", () => {
    const fused = {
      stream: "incident.fused",
      data: { candidate_id: "c-1", correlated_vessel_id: "42" },
    };
    const state = mergeSpillEvent({}, fused);
    expect(state["c-1"].correlated_vessel_id).toBe(42);
    expect(renderPopupHtml(state["c-1"])).toContain("42");
  });

  it("candidateFromEvent extracts the stable identity", () => {
    const c = candidateFromEvent(FILTERED, GEOMETRY);
    expect(c.candidate_id).toBe("c-1");
    expect(c.geometry).toEqual(GEOMETRY);
  });
});

describe("confidence colour mapping (clamped to [0.5, 1.0])", () => {
  it("0.5 maps to yellow", () => {
    expect(confidenceColor(0.5)).toBe(COLOR_YELLOW);
  });
  it("1.0 maps to red", () => {
    expect(confidenceColor(1.0)).toBe(COLOR_RED);
  });
  it("out-of-range values are clamped for visualisation only", () => {
    expect(confidenceColor(0.2)).toBe(COLOR_YELLOW);
    expect(confidenceColor(1.4)).toBe(COLOR_RED);
    expect(confidenceColor(0.75)).not.toBe(COLOR_YELLOW);
    expect(confidenceColor(0.75)).not.toBe(COLOR_RED);
  });
  it("missing confidence uses the fallback colour", () => {
    expect(confidenceColor(undefined)).toBe(COLOR_YELLOW);
  });
});

describe("synthetic candidate styling", () => {
  it("synthetic candidates use a dashed border and are distinct from real ones", () => {
    const syntheticStyle = candidateStyle({ is_synthetic: true, confidence: 0.9 });
    const realStyle = candidateStyle({ is_synthetic: false, confidence: 0.9 });
    expect(syntheticStyle.dashArray).toBe("6, 6");
    expect(realStyle.dashArray).toBeUndefined();
    expect(syntheticStyle.color).toBe(realStyle.color); // colour semantics unchanged
  });
});

describe("popup content", () => {
  const candidate = {
    ...FILTERED.data,
    geometry: GEOMETRY,
    area_m2: 186175.63,
    texture_features: { contrast: 27.6, energy: 0.72 },
  };

  it("synthetic popup contains a prominent SYNTHETIC DEMO banner", () => {
    const data = buildPopupData({ ...candidate, is_synthetic: true });
    expect(data.synthetic).toBe(true);
    expect(data.header).toBe("SYNTHETIC DEMO");
    expect(renderPopupHtml({ ...candidate, is_synthetic: true })).toContain("SYNTHETIC DEMO");
  });

  it("real candidates do not show SYNTHETIC DEMO", () => {
    expect(renderPopupHtml(candidate)).not.toContain("SYNTHETIC DEMO");
    expect(buildPopupData(candidate).synthetic).toBe(false);
  });

  it("popup shows confidence, classification, area and acquisition time", () => {
    const html = renderPopupHtml(candidate);
    expect(html).toContain("possible_oil_spill");
    expect(html).toContain("0.750");
    expect(html).toContain("186175.6 m²");
    expect(html).toContain("2024-08-20T09:40:00.000Z");
  });

  it("texture features are formatted readably, not as an opaque string", () => {
    const html = renderPopupHtml(candidate);
    expect(html).toContain("27.6");
    expect(html).toContain("0.72");
    expect(html).toContain("contrast");
  });

  it("correlated_vessel_id is displayed when present", () => {
    const html = renderPopupHtml({ ...candidate, correlated_vessel_id: 42 });
    expect(html).toContain("42");
  });

  it("a null correlated vessel shows an explicit state", () => {
    const html = renderPopupHtml({ ...candidate, correlated_vessel_id: null });
    expect(html).toContain("No correlated vessel");
  });

  it("never contains confirmed/ground-truth terminology", () => {
    expect(renderPopupHtml(candidate)).not.toMatch(/confirmed|confirmed_oil|oil_confirmed/i);
  });
});

describe("event routing", () => {
  it("routes the two spill streams", () => {
    expect(isSpillEvent(FILTERED)).toBe(true);
    expect(isSpillEvent(FUSED)).toBe(true);
    expect(isSpillEvent({ stream: "vessel.risk", data: {} })).toBe(false);
  });
});