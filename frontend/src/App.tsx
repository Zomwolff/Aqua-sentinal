import { useEffect, useRef, useState } from "react";
import maplibregl, { Map as MapLibreMap } from "maplibre-gl";

type Severity = "critical" | "high" | "medium" | "low";
type Vessel = { id: string; name: string; mmsi: string; type: string; risk: Severity; score: number; coordinates: [number, number]; detail: string };
type Incident = { id: string; title: string; location: string; age: string; severity: Severity; vessel: string; area: string; exposure: string; confidence: number; coordinates: [number, number] };
type FeedItem = { time: string; kind: "spill" | "risk" | "dark" | "system"; title: string; body: string };

const vessels: Vessel[] = [
  { id: "v-1", name: "MV Ocean Laurel", mmsi: "636019842", type: "Tanker", risk: "high", score: 82, coordinates: [72.72, 15.22], detail: "AIS trust dropped below the operating threshold" },
  { id: "v-2", name: "Sagar Kiran", mmsi: "419001637", type: "Cargo", risk: "medium", score: 48, coordinates: [72.38, 14.52], detail: "Course deviation detected near the spill perimeter" },
  { id: "v-3", name: "Blue Meridian", mmsi: "477194300", type: "Research", risk: "low", score: 16, coordinates: [73.36, 15.76], detail: "No active anomalies" },
  { id: "v-4", name: "Kestrel 8", mmsi: "563112900", type: "Unknown", risk: "critical", score: 94, coordinates: [72.92, 14.72], detail: "Broadcast identity inconsistent with observed track" },
];

const incidents: Incident[] = [
  { id: "INC-0247", title: "Surface anomaly", location: "Offshore Goa shelf", age: "14 min ago", severity: "critical", vessel: "MV Ocean Laurel", area: "18.6 km²", exposure: "High", confidence: 92, coordinates: [72.71, 15.2] },
  { id: "INC-0246", title: "Possible hydrocarbon slick", location: "North of Karwar", age: "42 min ago", severity: "high", vessel: "Unknown", area: "7.2 km²", exposure: "Medium", confidence: 77, coordinates: [74.02, 14.9] },
  { id: "INC-0244", title: "Low confidence anomaly", location: "Mormugao approach", age: "2 hr ago", severity: "medium", vessel: "Sagar Kiran", area: "2.8 km²", exposure: "Low", confidence: 64, coordinates: [73.64, 15.32] },
];

const feed: FeedItem[] = [
  { time: "09:42:18", kind: "spill", title: "Incident confidence increased", body: "INC-0247 crossed the critical threshold" },
  { time: "09:39:04", kind: "risk", title: "Vessel risk escalated", body: "MV Ocean Laurel · AIS trust 0.31" },
  { time: "09:36:51", kind: "dark", title: "Dark vessel sighted", body: "9.4 nm west of protected shelf" },
  { time: "09:31:27", kind: "system", title: "SAR pass ingested", body: "Sentinel-1 · 12 new evidence frames" },
];

const severityLabel: Record<Severity, string> = { critical: "Critical", high: "High", medium: "Medium", low: "Low" };

function App() {
  const mapNode = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const [selectedIncident, setSelectedIncident] = useState<Incident>(incidents[0]);
  const [selectedVessel, setSelectedVessel] = useState<Vessel | null>(null);
  const [horizon, setHorizon] = useState(0);
  const [layers, setLayers] = useState({ vessels: true, dark: true, protected: true });

  useEffect(() => {
    if (!mapNode.current) return;
    const map = new maplibregl.Map({
      container: mapNode.current,
      style: {
        version: 8,
        sources: {
          "openstreetmap": {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            attribution: "© OpenStreetMap contributors",
          },
        },
        layers: [
          { id: "ocean", type: "background", paint: { "background-color": "#18383b" } },
          { id: "openstreetmap-tiles", type: "raster", source: "openstreetmap", paint: { "raster-opacity": 0.86, "raster-saturation": -0.15 } },
        ],
      },
      center: [73.1, 15.1],
      zoom: 7.3,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    mapRef.current = map;
    map.on("load", () => {
      map.addSource("navigation-grid", { type: "geojson", data: { type: "FeatureCollection" as const, features: [
        ...[-1, -0.5, 0, 0.5, 1].map((offset) => ({ type: "Feature" as const, geometry: { type: "LineString" as const, coordinates: [[71.9, 15.1 + offset], [74.4, 15.1 + offset]] }, properties: {} })),
        ...[-1, -0.5, 0, 0.5, 1].map((offset) => ({ type: "Feature" as const, geometry: { type: "LineString" as const, coordinates: [[73.15 + offset, 14.05], [73.15 + offset, 16.15]] }, properties: {} })),
      ] } });
      map.addLayer({ id: "navigation-grid-lines", type: "line", source: "navigation-grid", paint: { "line-color": "#72a3a0", "line-opacity": 0.16, "line-width": 1, "line-dasharray": [2, 4] } });
      map.addSource("incident", { type: "geojson", data: { type: "Feature", geometry: { type: "Polygon", coordinates: [[[72.62, 15.08], [72.81, 15.1], [72.89, 15.29], [72.72, 15.37], [72.58, 15.24], [72.62, 15.08]]] }, properties: {} } });
      map.addLayer({ id: "incident-fill", type: "fill", source: "incident", paint: { "fill-color": "#df684b", "fill-opacity": 0.2 } });
      map.addLayer({ id: "incident-outline", type: "line", source: "incident", paint: { "line-color": "#f47f55", "line-width": 2, "line-dasharray": [2, 2] } });
      map.addSource("trajectory", { type: "geojson", data: { type: "Feature", geometry: { type: "LineString", coordinates: [[72.2, 14.75], [72.42, 14.92], [72.62, 15.1], [72.71, 15.2]] }, properties: {} } });
      map.addLayer({ id: "trajectory-line", type: "line", source: "trajectory", paint: { "line-color": "#f4bd68", "line-width": 2, "line-dasharray": [1, 2] } });
      map.addSource("vessels", { type: "geojson", data: { type: "FeatureCollection", features: vessels.map((vessel) => ({ type: "Feature", geometry: { type: "Point", coordinates: vessel.coordinates }, properties: { risk: vessel.risk, name: vessel.name } })) } });
      map.addLayer({ id: "vessel-points", type: "circle", source: "vessels", paint: { "circle-radius": 7, "circle-color": ["match", ["get", "risk"], "critical", "#ed684c", "high", "#ef9259", "medium", "#e5b75d", "#73bd9d"], "circle-stroke-color": "#f5efe2", "circle-stroke-width": 1.5 } });
      map.addSource("dark-vessel", { type: "geojson", data: { type: "Feature", geometry: { type: "Point", coordinates: [72.91, 14.72] }, properties: {} } });
      map.addLayer({ id: "dark-vessel-point", type: "circle", source: "dark-vessel", paint: { "circle-radius": 9, "circle-color": "#151c22", "circle-stroke-color": "#df684b", "circle-stroke-width": 2 } });
      map.addSource("protected", { type: "geojson", data: { type: "Feature", geometry: { type: "Polygon", coordinates: [[[73.42, 15.02], [73.8, 15.0], [73.85, 15.3], [73.45, 15.38], [73.42, 15.02]]] }, properties: {} } });
      map.addLayer({ id: "protected-fill", type: "fill", source: "protected", paint: { "fill-color": "#4e9a91", "fill-opacity": 0.16 } });
      map.addLayer({ id: "protected-outline", type: "line", source: "protected", paint: { "line-color": "#66b9a7", "line-width": 1, "line-dasharray": [3, 3] } });
      map.on("click", "vessel-points", (event) => {
        const feature = event.features?.[0];
        const name = feature?.properties?.name;
        const vessel = vessels.find((item) => item.name === name);
        if (vessel) setSelectedVessel(vessel);
      });
      map.on("mouseenter", "vessel-points", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "vessel-points", () => { map.getCanvas().style.cursor = ""; });
    });
    return () => { map.remove(); mapRef.current = null; };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    ["vessel-points", "dark-vessel-point", "protected-fill", "protected-outline"].forEach((id) => map.setLayoutProperty(id, "visibility", (id.startsWith("vessel") ? layers.vessels : id.startsWith("dark") ? layers.dark : layers.protected) ? "visible" : "none"));
  }, [layers]);

  const focusIncident = (incident: Incident) => {
    setSelectedIncident(incident);
    mapRef.current?.flyTo({ center: incident.coordinates, zoom: 8.4, duration: 1000 });
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><div className="brand-mark"><span /></div><div><strong>AQUA SENTINEL</strong><small>MARITIME INTELLIGENCE NETWORK</small></div></div>
        <div className="header-center"><span className="live-dot" /> <span>LIVE OPERATIONS</span><i /> <span className="muted">20 AUG 2026 · 09:42:18 UTC</span></div>
        <div className="header-meta"><div><small>ACTIVE INCIDENTS</small><b>03</b></div><div><small>VESSELS TRACKED</small><b>148</b></div><button className="icon-button" aria-label="Open settings">•••</button></div>
      </header>
      <main className="workspace">
        <section className="map-pane">
          <div className="map-frame"><div ref={mapNode} className="map-container" /></div>
          <div className="map-vignette" />
          <div className="map-label map-title"><small>OPERATIONAL PICTURE</small><h1>Konkan Coast / Sector 04</h1><span>Live vessel telemetry and fused SAR intelligence</span></div>
          <div className="map-legend"><small>LAYERS</small>{([['vessels', 'AIS vessels'], ['dark', 'Dark detections'], ['protected', 'Protected zones']] as const).map(([key, label]) => <button key={key} className={`legend-item ${layers[key] ? "active" : ""}`} onClick={() => setLayers((state) => ({ ...state, [key]: !state[key] }))}><span className={`legend-swatch ${key}`} />{label}</button>)}</div>
          <div className="map-scale"><span className="scale-line" /><span>10 nm</span></div>
          <div className="coordinate">15° 12' N &nbsp; 73° 04' E</div>
          <div className="forecast-bar"><div><small>FORECAST HORIZON</small><strong>{horizon === 0 ? "NOW" : `+${horizon}H`}</strong></div><div className="horizon-track">{[0, 6, 12, 24, 48, 72].map((value) => <button key={value} className={horizon === value ? "chosen" : ""} onClick={() => setHorizon(value)}><span>{value === 0 ? "Now" : `+${value}h`}</span></button>)}</div><div className="forecast-confidence"><small>MODEL CONFIDENCE</small><strong>{Math.max(48, 92 - horizon / 2)}%</strong></div></div>
        </section>
        <aside className="sidebar">
          <section className="side-section feed-section"><div className="section-head"><div><small>STREAM / 04</small><h2>Live signal feed</h2></div><span className="connection">CONNECTED</span></div><div className="feed-list">{feed.map((item) => <div className="feed-item" key={item.time}><span className={`feed-icon ${item.kind}`}>{item.kind === "spill" ? "!" : item.kind === "risk" ? "↗" : item.kind === "dark" ? "◌" : "·"}</span><div><b>{item.title}</b><p>{item.body}</p></div><time>{item.time}</time></div>)}</div></section>
          <section className="side-section incidents-section"><div className="section-head"><div><small>MONITORED EVENTS</small><h2>Active incidents <em>03</em></h2></div><button className="text-button">View archive <span>→</span></button></div><div className="incident-list">{incidents.map((incident) => <button className={`incident-row ${selectedIncident.id === incident.id ? "selected" : ""}`} key={incident.id} onClick={() => focusIncident(incident)}><span className={`severity-bar ${incident.severity}`} /><div className="incident-copy"><div><b>{incident.id}</b><span className={`severity-pill ${incident.severity}`}>{severityLabel[incident.severity]}</span></div><strong>{incident.title}</strong><p>{incident.location} <span>·</span> {incident.age}</p><small>Top attribution: <b>{incident.vessel}</b></small></div><span className="row-arrow">↗</span></button>)}</div></section>
        </aside>
      </main>
      <section className="detail-panel"><div className="detail-intro"><div><small>SELECTED INCIDENT</small><h2>{selectedIncident.id} <span className={`severity-pill ${selectedIncident.severity}`}>{severityLabel[selectedIncident.severity]}</span></h2><p>{selectedIncident.location} · detected {selectedIncident.age}</p></div><button className="close-button" aria-label="Close incident details">×</button></div><div className="confidence"><div><small>FUSION CONFIDENCE</small><strong>{selectedIncident.confidence}%</strong></div><div className="confidence-bar"><span style={{ width: `${selectedIncident.confidence}%` }} /></div><p>High confidence match across SAR, AIS and drift model evidence</p></div><div className="detail-stats"><div><small>EST. AREA</small><b>{selectedIncident.area}</b></div><div><small>ECOLOGICAL EXPOSURE</small><b className="exposure">{selectedIncident.exposure}</b></div><div><small>GROWTH RATE</small><b>+8.4% <small>/ HR</small></b></div></div><div className="attribution"><div className="section-head"><div><small>SOURCE ATTRIBUTION</small><h3>Probable origin vessels</h3></div><span className="info-mark">i</span></div><div className="attribution-row"><div><span>MV Ocean Laurel</span><b>87%</b></div><div className="attribution-track"><span style={{ width: "87%" }} /></div></div><div className="attribution-row muted-row"><div><span>Unknown source</span><b>08%</b></div><div className="attribution-track"><span style={{ width: "8%" }} /></div></div></div><div className="actions"><small>RECOMMENDED ACTIONS</small><ol><li>Deploy containment perimeter</li><li>Notify coastal authority</li><li>Track probable source vessel</li></ol></div></section>
      {selectedVessel && <div className="vessel-popover"><button onClick={() => setSelectedVessel(null)} aria-label="Close vessel details">×</button><small>VESSEL PROFILE</small><h3>{selectedVessel.name}</h3><p>{selectedVessel.type} · MMSI {selectedVessel.mmsi}</p><div className="vessel-risk"><span className={`severity-pill ${selectedVessel.risk}`}>{severityLabel[selectedVessel.risk]} risk</span><strong>{selectedVessel.score}<small>/100</small></strong></div><div className="sparkline"><i /><i /><i /><i /><i /><i /><i /><i /></div><p className="vessel-detail">{selectedVessel.detail}</p></div>}
    </div>
  );
}

export default App;
