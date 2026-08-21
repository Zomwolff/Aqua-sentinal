import { useEffect, useReducer, useRef } from "react";
import { GeoJSON } from "react-leaflet";

import {
  candidateStyle,
  initialLayerState,
  isSpillEvent,
  mergeSpillEvent,
  renderPopupHtml,
} from "../lib/spillEvents";
import { getSpillCandidate } from "../lib/apiClient";

function withGeometry(event, geometry) {
  if (!geometry) return event;
  return { ...event, data: { ...(event.data ?? {}), geometry } };
}

function reducer(state, action) {
  if (action.type === "merge") {
    const candidates = mergeSpillEvent(state.candidates, action.event);
    const geometryCache = { ...state.geometryCache };
    if (action.geometry) geometryCache[action.candidateId] = action.geometry;
    return { ...state, candidates, geometryCache };
  }
  if (action.type === "geometry-error") {
    return {
      ...state,
      geometryErrors: {
        ...state.geometryErrors,
        [action.candidateId]: action.error,
      },
    };
  }
  return state;
}

/**
 * Dedicated spill-candidate polygon layer.
 *
 * One polygon per candidate_id (stable identity). Both
 * `spill.candidates.filtered` and `incident.fused` events merge into the same
 * candidate record; incident.fused enriches it (e.g. correlated_vessel_id)
 * without creating a duplicate polygon. Geometry is resolved from the
 * api-gateway candidate endpoint (events do not carry polygons).
 */
export function SpillCandidateLayer({ event, fetcher = fetch, onSpillSelect }) {
  const [state, dispatch] = useReducer(reducer, null, initialLayerState);
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    if (!isSpillEvent(event)) return;
    const data = event.data ?? {};
    if (data.candidate_id === undefined || data.candidate_id === null) return;

    const candidateId = String(data.candidate_id);
    let cancelled = false;

    (async () => {
      const current = stateRef.current;
      const existing = current.candidates[candidateId];
      // WS events may (rarely) embed geometry; otherwise prefer the cache, then
      // the API. Geometry is never fabricated when all three are absent.
      const wsGeometry = data.geometry || null;
      const cached =
        wsGeometry || existing?.geometry || current.geometryCache[candidateId];

      if (cached) {
        dispatch({
          type: "merge",
          candidateId,
          event: withGeometry(event, cached),
          geometry: cached,
        });
        return;
      }

      try {
        const candidate = await getSpillCandidate(candidateId, fetcher);
        if (cancelled) return;
        const geometry = candidate.geometry || null;
        // The API returns the full record (geometry, area_m2, texture_features,
        // etc.). Merge those DB fields with the (fresher) WS event fields so the
        // polygon and its popup have complete metadata.
        const enriched = {
          ...event,
          data: { ...candidate, ...(event.data ?? {}), geometry },
        };
        dispatch({
          type: "merge",
          candidateId,
          event: enriched,
          geometry,
        });
      } catch (error) {
        if (cancelled) return;
        // No geometry available: keep metadata, skip polygon rendering.
        dispatch({ type: "merge", candidateId, event });
        dispatch({ type: "geometry-error", candidateId, error: String(error) });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [event, fetcher]); // eslint-disable-line react-hooks/exhaustive-deps

  const seen = new Set();
  const candidates = Object.values(state.candidates).filter((candidate) => {
    if (!candidate.geometry || seen.has(candidate.candidate_id)) return false;
    seen.add(candidate.candidate_id);
    return true;
  });

  return candidates.map((candidate) => (
    <GeoJSON
      key={candidate.candidate_id}
      data={candidate.geometry}
      style={() => candidateStyle(candidate)}
      onEachFeature={(_feature, layer) => {
        layer.bindPopup(renderPopupHtml(candidate));
      }}
      eventHandlers={{
        click: () => onSpillSelect?.(candidate.candidate_id),
      }}
    />
  ));
}