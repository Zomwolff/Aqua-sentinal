import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SpillCandidateLayer } from "../SpillCandidateLayer";

const layer = {
  bindPopup: (html) => {
    layer.popupHtml = html;
  },
  popupHtml: "",
};

vi.mock("react-leaflet", () => ({
  MapContainer: ({ children }) => <div>{children}</div>,
  TileLayer: () => null,
  GeoJSON: ({ data, onEachFeature }) => {
    layer.popupHtml = "";
    onEachFeature?.({}, layer);
    return (
      <div data-testid="geojson" data-bbox={JSON.stringify(data?.bbox ?? null)}>
        {layer.popupHtml}
      </div>
    );
  },
  CircleMarker: () => null,
  Popup: () => null,
}));

const GEOMETRY = {
  type: "Polygon",
  coordinates: [
    [
      [72.75, 19.15],
      [72.76, 19.15],
      [72.76, 19.16],
      [72.75, 19.16],
      [72.75, 19.15],
    ],
  ],
};

const CANDIDATE = {
  candidate_id: "any",
  geometry: GEOMETRY,
  texture_features: { contrast: 27.6 },
  area_m2: 186175.63,
};

const fetcher = () =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve(CANDIDATE),
  });

let sequence = 0;
function eventFactory(stream, candidateId, extra = {}) {
  sequence += 1;
  return {
    id: `${sequence}`,
    stream,
    data: {
      candidate_id: candidateId,
      confidence: 0.75,
      classification_label: "possible_oil_spill",
      is_synthetic: false,
      ...extra,
    },
  };
}

async function flush() {
  // The fetch path resolves geometry asynchronously; flush microtasks + a
  // macrotask so the resulting dispatch + re-render settle before assertions.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function mount(events) {
  const utils = render(<SpillCandidateLayer event={null} fetcher={fetcher} />);
  for (const event of events) {
    await act(async () => {
      utils.rerender(<SpillCandidateLayer event={event} fetcher={fetcher} />);
    });
    await flush();
  }
  return utils;
}

beforeEach(() => {
  layer.popupHtml = "";
});

describe("SpillCandidateLayer", () => {
  it("creates a polygon from a spill.candidates.filtered event", async () => {
    await mount([eventFactory("spill.candidates.filtered", "c-1")]);
    expect(screen.getAllByTestId("geojson")).toHaveLength(1);
  });

  it("creates one polygon per distinct candidate", async () => {
    await mount([
      eventFactory("spill.candidates.filtered", "c-1"),
      eventFactory("spill.candidates.filtered", "c-2"),
    ]);
    expect(screen.getAllByTestId("geojson")).toHaveLength(2);
  });

  it("repeated events for the same candidate do not create duplicate polygons", async () => {
    await mount([
      eventFactory("spill.candidates.filtered", "c-1", { confidence: 0.6 }),
      eventFactory("spill.candidates.filtered", "c-1", { confidence: 0.9 }),
      eventFactory("spill.candidates.filtered", "c-1", { confidence: 0.8 }),
    ]);
    expect(screen.getAllByTestId("geojson")).toHaveLength(1);
  });

  it("incident.fused enriches the same polygon rather than duplicating it", async () => {
    await mount([
      eventFactory("spill.candidates.filtered", "c-1"),
      eventFactory("incident.fused", "c-1", { correlated_vessel_id: 42 }),
    ]);
    expect(screen.getAllByTestId("geojson")).toHaveLength(1);
    expect(layer.popupHtml).toContain("42");
  });

  it("does not render polygons for non-spill events", async () => {
    await mount([eventFactory("vessel.risk", "c-9")]);
    expect(screen.queryAllByTestId("geojson")).toHaveLength(0);
  });

  it("populates area_m2 and texture_features from the API candidate fetch", async () => {
    await mount([eventFactory("spill.candidates.filtered", "c-1")]);
    expect(screen.getAllByTestId("geojson")).toHaveLength(1);
    // area_m2 186175.63 -> "186175.6 m²"; texture contrast 27.6 rendered readably
    expect(layer.popupHtml).toContain("186175.6 m²");
    expect(layer.popupHtml).toContain("27.6");
    expect(layer.popupHtml).toContain("contrast");
  });

  it("uses the WS geometry when present without calling the API", async () => {
    const spy = vi.fn(fetcher);
    const utils = render(<SpillCandidateLayer event={null} fetcher={spy} />);
    await act(async () => {
      utils.rerender(
        <SpillCandidateLayer
          event={eventFactory("spill.candidates.filtered", "c-1", { geometry: GEOMETRY })}
          fetcher={spy}
        />,
      );
    });
    expect(spy).not.toHaveBeenCalled();
    expect(screen.getAllByTestId("geojson")).toHaveLength(1);
  });
});