import { useEffect, useRef, useState } from "react";
import maplibregl, { Map as MapLibreMap } from "maplibre-gl";
import { fetchVessels, fetchIncidents, fetchIncidentDetail } from "./lib/api";
import { useLiveFeeds } from "./hooks/useLiveFeeds";
import { SARTaskingPipeline } from "./components/SARTaskingPipeline";
import { IncidentDetailsPage } from "./components/IncidentDetailsPage";

type Severity = "critical" | "high" | "medium" | "low";
type Vessel = { id: string; name: string; mmsi: string; type: string; risk: Severity; score: number; coordinates: [number, number]; detail: string };
type Incident = { rawId: string; id: string; title: string; location: string; age: string; severity: Severity; vessel: string; area: string; exposure: string; confidence: number; coordinates: [number, number] };
type FeedItem = { time: string; kind: "spill" | "risk" | "dark" | "system"; title: string; body: string };

const severityLabel: Record<Severity, string> = { critical: "Critical", high: "High", medium: "Medium", low: "Low" };

function App() {
  const mapNode = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const [selectedIncident, setSelectedIncident] = useState<Incident | null>(null);
  const [incidentDetail, setIncidentDetail] = useState<any>(null);
  const [selectedVessel, setSelectedVessel] = useState<Vessel | null>(null);
  const [horizon, setHorizon] = useState(0);
  const [layers, setLayers] = useState({ vessels: true, dark: true, protected: true });
  const [mapLoaded, setMapLoaded] = useState(false);

  const [historicalFeed, setHistoricalFeed] = useState<FeedItem[]>([]);
  const { feed, liveEvent } = useLiveFeeds(historicalFeed);
  const [vessels, setVessels] = useState<Vessel[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [activeCount, setActiveCount] = useState(0);
  const [historicalSceneId, setHistoricalSceneId] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"map" | "incident">("map");

  useEffect(() => {
    let mounted = true;
    async function loadData() {
      try {
        const vData = await fetchVessels();
        const iData = await fetchIncidents();
        
        let newFeed: FeedItem[] = [];

        if (mounted && vData.vessels) {
          setVessels(vData.vessels.map((v: any) => ({
             id: `v-${v.mmsi}`,
             name: v.vessel_name || `Vessel ${v.mmsi}`,
             mmsi: String(v.mmsi),
             type: v.vessel_type_str || "Unknown",
             risk: (v.risk_tier || "LOW").toLowerCase() as Severity,
             score: v.risk_score ? Math.round(v.risk_score) : 0,
             coordinates: [v.last_lon || 0, v.last_lat || 0],
             detail: v.risk_tier ? "Risk rules triggered" : "Normal tracking"
          })));

          // Add risk-flagged vessels to the historical feed
          vData.vessels.forEach((v: any) => {
            if (v.risk_tier && v.risk_tier !== "LOW") {
              const timeStr = new Date(v.last_seen || Date.now()).toLocaleTimeString("en-US", { hour12: false, timeZone: "UTC" });
              // Try to extract a specific reason if contributing_factors exist, else generic
              let reason = "Abnormal behavior detected";
              if (v.contributing_factors && v.contributing_factors.length > 0) {
                 reason = v.contributing_factors[0].factor.replace(/_/g, " ");
              }
              
              if (v.risk_tier === "CRITICAL") {
                newFeed.push({ time: timeStr, kind: "system", title: `SAR Process Triggered`, body: `Satellite tasked for CRITICAL vessel ${v.mmsi} - ${reason}` });
              } else {
                newFeed.push({ time: timeStr, kind: "risk", title: `Historical Risk: ${v.risk_tier}`, body: `Vessel ${v.mmsi} scored ${Math.round(v.risk_score)} due to ${reason}` });
              }
            }
          });
        }

        if (mounted && iData.incidents) {
           setActiveCount(iData.count);
           setIncidents(iData.incidents.map((i: any) => ({
              rawId: i.id,
              id: i.id.substring(0, 8),
              title: i.source === "synthetic" ? "Synthetic Slick" : "Surface Anomaly",
              location: `${i.latitude.toFixed(2)}N ${i.longitude.toFixed(2)}E`,
              age: new Date(i.detected_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
              severity: (i.severity_level || "LOW").toLowerCase() as Severity,
              vessel: i.top_vessel_mmsi ? `MMSI ${i.top_vessel_mmsi}` : "Unknown",
              area: `${(i.area_km2 || 0).toFixed(1)} km²`,
              exposure: i.severity_score > 0.5 ? "High" : "Low",
              confidence: i.confidence ? Math.round(i.confidence * 100) : 0,
              coordinates: [i.longitude, i.latitude]
           })));
           
           // Add incidents to historical feed
           iData.incidents.forEach((i: any) => {
              const timeStr = new Date(i.detected_at || Date.now()).toLocaleTimeString("en-US", { hour12: false, timeZone: "UTC" });
              newFeed.push({ time: timeStr, kind: "spill", title: "Historical Spill Detected", body: `Spill ${i.id.substring(0,8)} attributed to ${i.top_vessel_mmsi ? 'MMSI ' + i.top_vessel_mmsi : 'Unknown'}` });
           });
        }
        
        if (mounted && newFeed.length > 0) {
            // Sort combined feed by time (descending) and set it
            newFeed.sort((a, b) => b.time.localeCompare(a.time));
            setHistoricalFeed(newFeed.slice(0, 50));
        }
      } catch (err) {
        console.error(err);
      }
    }
    loadData();
    const timer = setInterval(loadData, 15000);
    return () => { mounted = false; clearInterval(timer); }
  }, []);

  // Handle real-time websocket events for instant vessel movement and state updates
  useEffect(() => {
    if (!liveEvent) return;
    if (liveEvent.type === "ais") {
        setVessels(curr => {
            const idx = curr.findIndex(v => v.mmsi === String(liveEvent.data.mmsi));
            if (idx === -1) return curr;
            const updated = [...curr];
            updated[idx] = {
                ...updated[idx],
                coordinates: [liveEvent.data.lon, liveEvent.data.lat]
            };
            return updated;
        });
    } else if (liveEvent.type === "risk") {
        setVessels(curr => {
            const idx = curr.findIndex(v => v.mmsi === String(liveEvent.data.mmsi));
            if (idx === -1) return curr;
            const updated = [...curr];
            updated[idx] = {
                ...updated[idx],
                risk: (liveEvent.data.tier || "LOW").toLowerCase() as Severity,
                score: liveEvent.data.score ? Math.round(liveEvent.data.score) : updated[idx].score,
            };
            return updated;
        });
    }
  }, [liveEvent]);

  // Keep selectedVessel in sync with vessels array updates
  useEffect(() => {
    if (selectedVessel) {
        const v = vessels.find(item => item.mmsi === selectedVessel.mmsi);
        if (v && (v.coordinates[0] !== selectedVessel.coordinates[0] || v.risk !== selectedVessel.risk)) {
            setSelectedVessel(v);
        }
    }
  }, [vessels, selectedVessel]);

  useEffect(() => {
    if (!mapLoaded || !mapRef.current) return;
    const source = mapRef.current.getSource("vessels") as maplibregl.GeoJSONSource;
    if (source) {
      source.setData({
        type: "FeatureCollection",
        features: vessels.filter(v => v.coordinates[0] !== 0).map(v => ({
          type: "Feature",
          geometry: { type: "Point", coordinates: v.coordinates },
          properties: { risk: v.risk, name: v.name, mmsi: v.mmsi }
        }))
      });
    }
  }, [vessels, mapLoaded]);

  useEffect(() => {
    if (!selectedIncident || !mapLoaded) return;
    let mounted = true;
    (async () => {
      try {
        const detail = await fetchIncidentDetail(selectedIncident.rawId);
        if (mounted) {
            setIncidentDetail(detail);
            if (detail.incident?.source_image_id) {
                setHistoricalSceneId(detail.incident.source_image_id);
            } else {
                setHistoricalSceneId(null);
            }
        }

        if (mapRef.current) {
          const incSource = mapRef.current.getSource("incident") as maplibregl.GeoJSONSource;
          if (incSource && detail.incident?.geometry) {
             incSource.setData({ type: "Feature", geometry: detail.incident.geometry, properties: {} });
          }
          const trajSource = mapRef.current.getSource("trajectory") as maplibregl.GeoJSONSource;
          if (trajSource && detail.forecasts) {
             const f = detail.forecasts.find((f: any) => f.horizon_hours === horizon) || detail.forecasts[0];
             if (f?.geometry) {
                 trajSource.setData({ type: "Feature", geometry: f.geometry, properties: {} });
             } else {
                 trajSource.setData({ type: "FeatureCollection", features: [] });
             }
          }
        }
      } catch (e) {}
    })();
    return () => { mounted = false; }
  }, [selectedIncident, horizon, mapLoaded]);

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
      center: [72.8, 19.0],
      zoom: 8.5,
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
      map.addSource("incident", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "incident-fill", type: "fill", source: "incident", paint: { "fill-color": "#df684b", "fill-opacity": 0.2 } });
      map.addLayer({ id: "incident-outline", type: "line", source: "incident", paint: { "line-color": "#f47f55", "line-width": 2, "line-dasharray": [2, 2] } });
      map.addSource("trajectory", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "trajectory-line", type: "line", source: "trajectory", paint: { "line-color": "#f4bd68", "line-width": 2, "line-dasharray": [1, 2] } });
      map.addSource("vessels", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "vessel-points", type: "circle", source: "vessels", paint: { "circle-radius": 7, "circle-color": ["match", ["get", "risk"], "critical", "#ed684c", "high", "#ef9259", "medium", "#e5b75d", "#73bd9d"], "circle-stroke-color": "#f5efe2", "circle-stroke-width": 1.5 } });
      
      // SAR radar ring for critical vessels
      map.addLayer({ 
        id: "vessel-sar-ring", 
        type: "circle", 
        source: "vessels", 
        filter: ["==", ["get", "risk"], "critical"],
        paint: { 
            "circle-radius": 14, 
            "circle-color": "transparent", 
            "circle-stroke-color": "#ed684c", 
            "circle-stroke-width": 2,
            "circle-stroke-opacity": 0.8
        } 
      }, "vessel-points");

      map.addSource("protected", { type: "geojson", data: { type: "Feature", geometry: { type: "Polygon", coordinates: [[[73.42, 15.02], [73.8, 15.0], [73.85, 15.3], [73.45, 15.38], [73.42, 15.02]]] }, properties: {} } });
      map.addLayer({ id: "protected-fill", type: "fill", source: "protected", paint: { "fill-color": "#4e9a91", "fill-opacity": 0.16 } });
      map.addLayer({ id: "protected-outline", type: "line", source: "protected", paint: { "line-color": "#66b9a7", "line-width": 1, "line-dasharray": [3, 3] } });
      
      map.on("click", "vessel-points", (event) => {
        const feature = event.features?.[0];
        const mmsi = feature?.properties?.mmsi;
        setVessels(curr => {
            const v = curr.find((item) => item.mmsi === String(mmsi));
            if (v) setSelectedVessel(v);
            return curr;
        });
      });
      map.on("mouseenter", "vessel-points", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "vessel-points", () => { map.getCanvas().style.cursor = ""; });
      
      setMapLoaded(true);
    });
    return () => { map.remove(); mapRef.current = null; };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    ["vessel-points", "vessel-sar-ring", "protected-fill", "protected-outline"].forEach((id) => {
        if (map.getLayer(id)) {
            map.setLayoutProperty(id, "visibility", (id.startsWith("vessel") ? layers.vessels : layers.protected) ? "visible" : "none");
        }
    });
  }, [layers]);

  const focusIncident = (incident: Incident) => {
    setSelectedIncident(incident);
    setViewMode("incident");
    mapRef.current?.flyTo({ center: incident.coordinates, zoom: 8.4, duration: 1000 });
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><div className="brand-mark"><span /></div><div><strong>AQUA SENTINEL</strong><small>MARITIME INTELLIGENCE NETWORK</small></div></div>
        <div className="header-center"><span className="live-dot" /> <span>LIVE OPERATIONS</span><i /> <span className="muted">{new Date().toUTCString()}</span></div>
        <div className="header-meta"><div><small>ACTIVE INCIDENTS</small><b>{activeCount < 10 ? `0${activeCount}` : activeCount}</b></div><div><small>VESSELS TRACKED</small><b>{vessels.length}</b></div><button className="icon-button" aria-label="Open settings">•••</button></div>
      </header>
      <main className="workspace">
        <section className="map-pane">
          <div className="map-frame"><div ref={mapNode} className="map-container" /></div>
          <div className="map-vignette" />
          <div className="map-label map-title"><small>OPERATIONAL PICTURE</small><h1>Konkan Coast / Sector 04</h1><span>Live vessel telemetry and fused SAR intelligence</span></div>
          <div className="map-legend"><small>LAYERS</small>{([['vessels', 'AIS vessels'], ['protected', 'Protected zones']] as const).map(([key, label]) => <button key={key} className={`legend-item ${layers[key as keyof typeof layers] ? "active" : ""}`} onClick={() => setLayers((state) => ({ ...state, [key]: !state[key as keyof typeof layers] }))}><span className={`legend-swatch ${key}`} />{label}</button>)}</div>
          <div className="map-scale"><span className="scale-line" /><span>10 nm</span></div>
          {selectedIncident && <div className="coordinate">{selectedIncident.coordinates[1].toFixed(2)}° N &nbsp; {selectedIncident.coordinates[0].toFixed(2)}° E</div>}
          <div className="forecast-bar"><div><small>FORECAST HORIZON</small><strong>{horizon === 0 ? "NOW" : `+${horizon}H`}</strong></div><div className="horizon-track">{[0, 3, 6, 12, 24].map((value) => <button key={value} className={horizon === value ? "chosen" : ""} onClick={() => setHorizon(value)}><span>{value === 0 ? "Now" : `+${value}h`}</span></button>)}</div><div className="forecast-confidence"><small>MODEL CONFIDENCE</small><strong>{Math.max(48, 92 - horizon / 2)}%</strong></div></div>
        </section>
        <aside className="sidebar">
          <section className="side-section feed-section">
             <div className="section-head"><div><small>STREAM / 04</small><h2>Live signal feed</h2></div><span className="connection">CONNECTED</span></div>
             <div className="feed-list">
                {feed.map((item, idx) => <div className="feed-item" key={idx}><span className={`feed-icon ${item.kind}`}>{item.kind === "spill" ? "!" : item.kind === "risk" ? "↗" : item.kind === "dark" ? "◌" : "·"}</span><div><b>{item.title}</b><p>{item.body}</p></div><time>{item.time}</time></div>)}
             </div>
          </section>
          <section className="side-section incidents-section">
             <div className="section-head"><div><small>MONITORED EVENTS</small><h2>Active incidents <em>{incidents.length < 10 ? `0${incidents.length}` : incidents.length}</em></h2></div><button className="text-button">View archive <span>→</span></button></div>
             <div className="incident-list">
                {incidents.map((incident) => <button className={`incident-row ${selectedIncident?.id === incident.id ? "selected" : ""}`} key={incident.id} onClick={() => focusIncident(incident)}><span className={`severity-bar ${incident.severity}`} /><div className="incident-copy"><div><b>{incident.id}</b><span className={`severity-pill ${incident.severity}`}>{severityLabel[incident.severity]}</span></div><strong>{incident.title}</strong><p>{incident.location} <span>·</span> {incident.age}</p><small>Top attribution: <b>{incident.vessel}</b></small></div><span className="row-arrow">↗</span></button>)}
             </div>
          </section>
        </aside>
      </main>
      
      {viewMode === "incident" && (
         <IncidentDetailsPage 
            incidentDetail={incidentDetail} 
            onBack={() => {
               setViewMode("map");
               setSelectedIncident(null);
               setIncidentDetail(null);
            }} 
         />
      )}
      
      {selectedVessel && (
      <div className="vessel-popover">
         <button onClick={() => setSelectedVessel(null)} aria-label="Close vessel details">×</button>
         <small>VESSEL PROFILE</small>
         <h3>{selectedVessel.name}</h3>
         <p>{selectedVessel.type} · MMSI {selectedVessel.mmsi}</p>
         <div className="vessel-risk">
             <span className={`severity-pill ${selectedVessel.risk}`}>{severityLabel[selectedVessel.risk]} risk</span>
             <strong>{selectedVessel.score}<small>/100</small></strong>
         </div>
         {selectedVessel.risk === "critical" && (
             <div style={{ marginTop: "12px", color: "#ed684c", fontWeight: 600, fontSize: "11px", display: "flex", alignItems: "center", gap: "6px" }}>
                <span className="live-dot" style={{ background: "#ed684c" }} /> SATELLITE TASKING REQUESTED
             </div>
         )}
         <div className="sparkline"><i /><i /><i /><i /><i /><i /><i /><i /></div>
         <p className="vessel-detail">{selectedVessel.detail}</p>
      </div>
      )}

      <SARTaskingPipeline liveEvent={liveEvent} historicalSceneId={historicalSceneId} />
    </div>
  );
}

export default App;
