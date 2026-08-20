import { useEffect, useState } from "react";
import { CircleMarker, Popup } from "react-leaflet";

import { getVessel } from "../lib/apiClient";

const TIER_COLORS = {
  LOW: "#22c55e",
  MEDIUM: "#f59e0b",
  HIGH: "#f97316",
  CRITICAL: "#dc2626",
};

/**
 * AIS vessel markers from `vessel.risk` events (WS type "risk").
 *
 * Risk payloads carry mmsi/tier but no position, so the marker position is
 * resolved via the existing api-gateway /vessels/{mmsi} endpoint. This keeps
 * the existing vessel.risk broadcast working on the map without a second
 * WebSocket connection.
 */
export function VesselLayer({ event, fetcher = fetch }) {
  const [vessels, setVessels] = useState({});

  useEffect(() => {
    if (!event || event.type !== "risk") return;
    const data = event.data ?? {};
    if (!data.mmsi) return;

    const mmsi = String(data.mmsi);
    let cancelled = false;

    (async () => {
      try {
        const vessel = await getVessel(mmsi, fetcher);
        if (cancelled) return;
        const pos = vessel.position || vessel;
        const lat = pos.last_lat ?? pos.lat;
        const lon = pos.last_lon ?? pos.lon;
        if (lat === undefined || lon === undefined) return;
        setVessels((prev) => ({
          ...prev,
          [mmsi]: {
            mmsi,
            tier: data.tier || vessel.tier || "LOW",
            risk_score: data.risk_score ?? vessel.risk_score,
            recommended_action: data.recommended_action,
            lat,
            lon,
          },
        }));
      } catch {
        /* vessel lookup failed — skip marker, never throw out of the map */
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [event, fetcher]); // eslint-disable-line react-hooks/exhaustive-deps

  return Object.values(vessels).map((vessel) => (
    <CircleMarker
      key={vessel.mmsi}
      center={[vessel.lat, vessel.lon]}
      radius={8}
      pathOptions={{
        color: "white",
        weight: 1,
        fillColor: TIER_COLORS[vessel.tier] ?? "#94a3b8",
        fillOpacity: 0.9,
      }}
    >
      <Popup>
        <div className="vessel-popup">
          <strong>Vessel {vessel.mmsi}</strong>
          <div>Risk: {vessel.tier}</div>
          {vessel.risk_score !== undefined && (
            <div>Score: {Number(vessel.risk_score).toFixed(1)}</div>
          )}
          {vessel.recommended_action && (
            <div>Action: {vessel.recommended_action}</div>
          )}
        </div>
      </Popup>
    </CircleMarker>
  ));
}