import { useEffect, useRef, useState, type FormEvent } from "react";
import maplibregl, { Map as MapLibreMap } from "maplibre-gl";
import {
  fetchVessels, fetchIncidents, fetchIncidentDetail, fetchVesselDetail,
  fetchVesselTrack, triggerLiveAisFetch, fetchProtectedAreas,
  fetchDarkVessels, fetchStsEvents, fetchSpoofingSuspects,
  uploadSarImage,
} from "./lib/api";
import { useLiveFeeds } from "./hooks/useLiveFeeds";
import { SARTaskingPipeline } from "./components/SARTaskingPipeline";
import { IncidentDetailsPage } from "./components/IncidentDetailsPage";
import { VesselDetailsPage } from "./components/VesselDetailsPage";
import { spillForecastDemo } from "./demo/spillForecastDemo";

type Severity = "critical" | "high" | "medium" | "low";
type Vessel = { id: string; name: string; mmsi: string; type: string; risk: Severity; score: number; coordinates: [number, number]; detail: string; sar_status?: string | null; detected_spill_id?: string | null };
type Incident = { rawId: string; id: string; title: string; location: string; age: string; severity: Severity; vessel: string; area: string; exposure: string; confidence: number; coordinates: [number, number] };
type FeedItem = { time: string; kind: "spill" | "risk" | "dark" | "system"; title: string; body: string };

const severityLabel: Record<Severity, string> = { critical: "Critical", high: "High", medium: "Medium", low: "Low" };
const isSpillForecastDemo = new URLSearchParams(window.location.search).get("demo") === "spill-forecast";

// Fallback protected zone (Konkan sector) used only until the DB-backed
// /protected-areas endpoint responds; replaced by real geometries on load.
const PROTECTED_FALLBACK: any = {
  type: "Feature",
  geometry: { type: "Polygon", coordinates: [[[73.42, 15.02], [73.8, 15.0], [73.85, 15.3], [73.45, 15.38], [73.42, 15.02]]] },
  properties: {},
};

function App() {
  const mapNode = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const [selectedIncident, setSelectedIncident] = useState<Incident | null>(isSpillForecastDemo ? spillForecastDemo.summary as unknown as Incident : null);
  const [incidentDetail, setIncidentDetail] = useState<any>(isSpillForecastDemo ? spillForecastDemo.detail : null);
  const [selectedVessel, setSelectedVessel] = useState<Vessel | null>(null);
  const [horizon, setHorizon] = useState(0);
  const [layers, setLayers] = useState({ vessels: true, dark: true, protected: true });
  const [mapLoaded, setMapLoaded] = useState(false);

  const [historicalFeed, setHistoricalFeed] = useState<FeedItem[]>([]);
  const { feed, liveEvent, connectionStatus } = useLiveFeeds(historicalFeed);
  const [vessels, setVessels] = useState<Vessel[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>(isSpillForecastDemo ? [spillForecastDemo.summary as unknown as Incident] : []);
  const [activeCount, setActiveCount] = useState(isSpillForecastDemo ? 1 : 0);
  const [historicalSceneId, setHistoricalSceneId] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"map" | "incident" | "vessel">(isSpillForecastDemo ? "incident" : "map");
  const [vesselDetail, setVesselDetail] = useState<any>(null);
  const [aisFetching, setAisFetching] = useState(false);
  const [sarUploadOpen, setSarUploadOpen] = useState(false);
  const [sarUploadMmsi, setSarUploadMmsi] = useState("");
  const [sarUploadFile, setSarUploadFile] = useState<File | null>(null);
  const [sarUploading, setSarUploading] = useState(false);
  const [sarUploadError, setSarUploadError] = useState("");
  const [protectedAreas, setProtectedAreas] = useState<any[]>([]);
  const [darkVessels, setDarkVessels] = useState<any[]>([]);
  const [intelSts, setIntelSts] = useState<any[]>([]);
  const [intelSpoof, setIntelSpoof] = useState<any[]>([]);
  const [trackPoints, setTrackPoints] = useState<{ lon: number; lat: number; speed: number | null }[]>([]);
  const [attributionMapLink, setAttributionMapLink] = useState<{ spill: [number, number]; vessel: [number, number]; distanceKm: number; mmsi: string } | null>(null);

  useEffect(() => {
    if (isSpillForecastDemo) { setHistoricalFeed([{ time: "09:38:00", kind: "spill", title: "Forecast demo loaded", body: "Frontend-only test data · no backend required" }]); return; }
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
             type: v.vessel_type || "Unknown",
             risk: (v.risk_tier || "LOW").toLowerCase() as Severity,
             score: v.risk_score ? Math.round(v.risk_score) : 0,
             coordinates: [v.last_lon || 0, v.last_lat || 0],
             detail: v.risk_tier ? "Risk rules triggered" : "Normal tracking",
             sar_status: v.sar_status || null,
             detected_spill_id: v.detected_spill_id || null
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
        // Keep the open vessel's track/sparkline current.
        if (selectedVessel && String(liveEvent.data.mmsi) === selectedVessel.mmsi && liveEvent.data.speed_knots !== undefined) {
            setTrackPoints(prev => [...prev, { lon: Number(liveEvent.data.lon), lat: Number(liveEvent.data.lat), speed: liveEvent.data.speed_knots === "" ? null : Number(liveEvent.data.speed_knots) }].slice(-60));
        }
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
    } else if (liveEvent.type === "dark_vessel") {
        // A new dark-vessel detection arrived — refresh the layer data.
        fetchDarkVessels(24).then((d: any) => setDarkVessels(d.dark_vessels || [])).catch(() => {});
    } else if (liveEvent.type === "ais_fetch") {
        // Fetch cycle completed — the button can be re-enabled.
        setAisFetching(false);
    }
  }, [liveEvent]);

  // Reference layers & intelligence panels: dark vessels / STS / spoofing
  // refresh on a slow poll; protected areas load once (they are static).
  useEffect(() => {
    let mounted = true;
    fetchProtectedAreas()
      .then((d: any) => { if (mounted) setProtectedAreas(d.protected_areas || []); })
      .catch(() => {});
    const loadIntel = () => {
      fetchDarkVessels(24).then((d: any) => { if (mounted) setDarkVessels(d.dark_vessels || []); }).catch(() => {});
      fetchStsEvents(48).then((d: any) => { if (mounted) setIntelSts(d.events || d.sts_events || []); }).catch(() => {});
      fetchSpoofingSuspects(0.5).then((d: any) => { if (mounted) setIntelSpoof(d.suspects || []); }).catch(() => {});
    };
    loadIntel();
    const timer = setInterval(loadIntel, 30000);
    return () => { mounted = false; clearInterval(timer); };
  }, []);

  // Keep selectedVessel in sync with vessels array updates
  useEffect(() => {
    if (selectedVessel) {
        const v = vessels.find(item => item.mmsi === selectedVessel.mmsi);
        if (v && (v.coordinates[0] !== selectedVessel.coordinates[0] || v.risk !== selectedVessel.risk)) {
            setSelectedVessel(v);
        }
    }
  }, [vessels, selectedVessel]);

  // Keep an uploaded SAR task visible as it advances through processing and
  // allow downstream fusion/severity services time to attach their results.
  useEffect(() => {
    if (viewMode !== "vessel" || !selectedVessel || !vesselDetail?.sar_tasking) return;
    const startedAt = Date.now();
    const poll = async () => {
      try {
        const fresh = await fetchVesselDetail(selectedVessel.mmsi);
        setVesselDetail(fresh);
        if (fresh.verdict?.status === "spill_detected" || Date.now() - startedAt > 90_000) {
          clearInterval(timer);
        }
      } catch {
        // A transient refresh failure should not interrupt the active pipeline.
      }
    };
    const timer = window.setInterval(poll, 2500);
    return () => window.clearInterval(timer);
  }, [viewMode, selectedVessel?.mmsi, vesselDetail?.sar_tasking?.scene_id]);

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
    if (isSpillForecastDemo) return;
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
             const f = horizon === 0 ? null : detail.forecasts.find((f: any) => Number(f.horizon_hours) === horizon);
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
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "nautical" }), "bottom-left");
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

      map.addSource("protected", { type: "geojson", data: PROTECTED_FALLBACK });
      map.addLayer({ id: "protected-fill", type: "fill", source: "protected", paint: { "fill-color": "#4e9a91", "fill-opacity": 0.16 } });
      map.addLayer({ id: "protected-outline", type: "line", source: "protected", paint: { "line-color": "#66b9a7", "line-width": 1, "line-dasharray": [3, 3] } });

      // Dark-vessel detections (AIS-gap analysis + SAR correlation)
      map.addSource("dark-vessels", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "dark-vessel-halo", type: "circle", source: "dark-vessels",
        paint: { "circle-radius": 16, "circle-color": "rgba(237,104,76,0.08)", "circle-stroke-color": "#ed684c", "circle-stroke-width": 1, "circle-stroke-opacity": 0.3 }
      });
      map.addLayer({
        id: "dark-vessel-points", type: "circle", source: "dark-vessels",
        paint: { "circle-radius": 5, "circle-color": "#192423", "circle-stroke-color": "#ed684c", "circle-stroke-width": 1.5 }
      });

      // Historical track of the selected vessel
      map.addSource("vessel-track", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "vessel-track-line", type: "line", source: "vessel-track",
        paint: { "line-color": "#7fd0e8", "line-width": 2, "line-opacity": 0.85 }
      });
      map.addLayer({
        id: "vessel-track-dots", type: "circle", source: "vessel-track",
        paint: { "circle-radius": 2.5, "circle-color": "#7fd0e8", "circle-opacity": 0.7 }
      });

      // Pulsing highlight ring around a spotted/selected vessel
      map.addSource("spot-highlight", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "spot-highlight-ring", type: "circle", source: "spot-highlight",
        paint: { "circle-radius": 22, "circle-color": "transparent", "circle-stroke-color": "#f4bd68", "circle-stroke-width": 2.5, "circle-stroke-opacity": 0.9 }
      });

      map.addSource("attribution-link", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "attribution-link-line", type: "line", source: "attribution-link", filter: ["==", ["geometry-type"], "LineString"], paint: { "line-color": "#f4bd68", "line-width": 2, "line-dasharray": [2, 2], "line-opacity": .9 } });
      map.addLayer({ id: "attribution-spill-point", type: "circle", source: "attribution-link", filter: ["==", ["get", "kind"], "spill"], paint: { "circle-radius": 9, "circle-color": "#ed7657", "circle-stroke-color": "#ffe1d7", "circle-stroke-width": 2 } });
      
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
    // Dark-vessel layer toggle now controls the real detection markers.
    ["dark-vessel-points", "dark-vessel-halo"].forEach((id) => {
        if (map.getLayer(id)) {
            map.setLayoutProperty(id, "visibility", layers.dark ? "visible" : "none");
        }
    });
  }, [layers]);

  // Push fetched protected areas into the map (replaces the fallback polygon
  // once real geometries arrive; keeps the fallback if the endpoint is empty).
  useEffect(() => {
    if (!mapLoaded || !mapRef.current || protectedAreas.length === 0) return;
    const src = mapRef.current.getSource("protected") as maplibregl.GeoJSONSource;
    if (!src) return;
    const features = protectedAreas
      .filter((p: any) => p.geometry)
      .map((p: any) => ({ type: "Feature", geometry: p.geometry, properties: { name: p.name, type: p.type } }));
    if (features.length > 0) src.setData({ type: "FeatureCollection", features } as any);
  }, [protectedAreas, mapLoaded]);

  // Push dark-vessel detections into their layer.
  useEffect(() => {
    if (!mapLoaded || !mapRef.current) return;
    const src = mapRef.current.getSource("dark-vessels") as maplibregl.GeoJSONSource;
    if (!src) return;
    src.setData({
      type: "FeatureCollection",
      features: darkVessels
        .filter(d => d.latitude != null && d.longitude != null)
        .map(d => ({
          type: "Feature",
          geometry: { type: "Point", coordinates: [Number(d.longitude), Number(d.latitude)] },
          properties: { mmsi: d.matched_mmsi || "", sensor: d.sensor || "" },
        })),
    });
  }, [darkVessels, mapLoaded]);

  // Draw the selected vessel's historical track + spot-highlight ring.
  useEffect(() => {
    if (!mapLoaded || !mapRef.current) return;
    const trackSrc = mapRef.current.getSource("vessel-track") as maplibregl.GeoJSONSource;
    const spotSrc = mapRef.current.getSource("spot-highlight") as maplibregl.GeoJSONSource;
    const validTrack = trackPoints.filter(p => Number.isFinite(p.lon) && Number.isFinite(p.lat));
    if (trackSrc && selectedVessel && validTrack.length >= 2) {
      trackSrc.setData({
        type: "FeatureCollection",
        features: [{
          type: "Feature",
          geometry: { type: "LineString", coordinates: validTrack.map(p => [p.lon, p.lat]) },
          properties: {},
        }],
      });
    } else if (trackSrc) {
      trackSrc.setData({ type: "FeatureCollection", features: [] });
    }
    if (spotSrc && selectedVessel) {
      spotSrc.setData({
        type: "FeatureCollection",
        features: [{
          type: "Feature",
          geometry: { type: "Point", coordinates: selectedVessel.coordinates },
          properties: {},
        }],
      });
    } else if (spotSrc) {
      spotSrc.setData({ type: "FeatureCollection", features: [] });
    }
  }, [selectedVessel, trackPoints, mapLoaded]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapLoaded) return;
    const source = map.getSource("attribution-link") as maplibregl.GeoJSONSource;
    if (!source) return;
    source.setData(attributionMapLink ? {
      type: "FeatureCollection",
      features: [
        { type: "Feature", geometry: { type: "LineString", coordinates: [attributionMapLink.spill, attributionMapLink.vessel] }, properties: { kind: "link" } },
        { type: "Feature", geometry: { type: "Point", coordinates: attributionMapLink.spill }, properties: { kind: "spill" } },
      ],
    } : { type: "FeatureCollection", features: [] });
  }, [attributionMapLink, mapLoaded]);

  const loadVesselTrack = async (mmsi: string) => {
    try {
      const data = await fetchVesselTrack(mmsi, 24);
      const points = (data.track || data.positions || []).map((p: any) => ({
        lon: Number(p.lon ?? p.longitude),
        lat: Number(p.lat ?? p.latitude),
        speed: p.speed_knots != null && p.speed_knots !== "" ? Number(p.speed_knots) : null,
      })).filter((p: any) => Number.isFinite(p.lon) && Number.isFinite(p.lat));
      setTrackPoints(points);
    } catch {
      setTrackPoints([]);
    }
  };

  const focusIncident = async (incident: Incident) => {
    setAttributionMapLink(null);
    setSelectedIncident(incident);
    setSelectedVessel(null);
    setTrackPoints([]);
    setViewMode("incident");
    mapRef.current?.flyTo({ center: incident.coordinates, zoom: 8.4, duration: 1000 });
    try {
       const data = await fetchIncidentDetail(incident.rawId);
       setIncidentDetail(data);
    } catch (e) {
       console.error(e);
    }
  };

  const focusFlaggedVessel = async (vessel: Vessel) => {
    setAttributionMapLink(null);
    setSelectedVessel(vessel);
    setSelectedIncident(null);
    setViewMode("vessel");
    mapRef.current?.flyTo({ center: vessel.coordinates, zoom: 8.4, duration: 1000 });
    loadVesselTrack(vessel.mmsi);
    try {
       const data = await fetchVesselDetail(vessel.mmsi);
       setVesselDetail(data);
    } catch (e) {
       console.error(e);
    }
  };

  // "Spot this vessel on the map" from an opened incident: jump back to the
  // operational picture centered on the attributed vessel with its popover.
  const spotVesselOnMap = async (mmsi: string) => {
    const spillCoordinates = selectedIncident?.coordinates;
    let vessel = vessels.find(v => v.mmsi === String(mmsi));
    if (!vessel) {
      try {
        const d = await fetchVesselDetail(mmsi);
        const lon = d.vessel?.last_lon;
        const lat = d.vessel?.last_lat;
        if (lon == null || lat == null) return;
        vessel = {
          id: `v-${mmsi}`,
          name: d.vessel?.name || `Vessel ${mmsi}`,
          mmsi: String(mmsi),
          type: d.vessel?.vessel_type || "Unknown",
          risk: ((d.risk?.tier || "LOW").toLowerCase()) as Severity,
          score: Math.round(d.risk?.score || 0),
          coordinates: [Number(lon), Number(lat)],
          detail: "Located from incident attribution",
        };
        setVessels(curr => (curr.some(v => v.mmsi === vessel!.mmsi) ? curr : [...curr, vessel!]));
      } catch {
        return;
      }
    }
    setSelectedIncident(null);
    setIncidentDetail(null);
    setSelectedVessel(vessel);
    setViewMode("map");
    loadVesselTrack(vessel.mmsi);
    if (spillCoordinates) {
      const [spillLon, spillLat] = spillCoordinates;
      const [vesselLon, vesselLat] = vessel.coordinates;
      const radians = (degrees: number) => degrees * Math.PI / 180;
      const dLat = radians(vesselLat - spillLat), dLon = radians(vesselLon - spillLon);
      const a = Math.sin(dLat / 2) ** 2 + Math.cos(radians(spillLat)) * Math.cos(radians(vesselLat)) * Math.sin(dLon / 2) ** 2;
      const distanceKm = 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
      setAttributionMapLink({ spill: spillCoordinates, vessel: vessel.coordinates, distanceKm, mmsi: vessel.mmsi });
      const bounds = new maplibregl.LngLatBounds(spillCoordinates, spillCoordinates).extend(vessel.coordinates);
      mapRef.current?.fitBounds(bounds, { padding: 120, maxZoom: 10, duration: 1200 });
    } else {
      mapRef.current?.flyTo({ center: vessel.coordinates, zoom: 9.2, duration: 1200 });
    }
  };

  // Manual live-AIS fetch: the reader interrupts its polling sleep and the
  // resulting positions stream into the live feed via /live.
  const handleFetchAis = async () => {
    if (aisFetching) return;
    setAisFetching(true);
    try { await triggerLiveAisFetch(); } catch { setAisFetching(false); }
    // Re-enable after a grace period even if no ais_fetch event arrives
    // (e.g. provider misconfigured), so the button never sticks disabled.
    setTimeout(() => setAisFetching(false), 15000);
  };

  const openSarUpload = () => {
    setSarUploadMmsi(selectedVessel?.mmsi || vessels[0]?.mmsi || "");
    setSarUploadFile(null);
    setSarUploadError("");
    setSarUploadOpen(true);
  };

  const handleSarUpload = async (event: FormEvent) => {
    event.preventDefault();
    if (!sarUploadMmsi || !sarUploadFile || sarUploading) return;
    setSarUploading(true);
    setSarUploadError("");
    try {
      await uploadSarImage(sarUploadMmsi, sarUploadFile);
      const target = vessels.find((v) => v.mmsi === sarUploadMmsi);
      setSarUploadOpen(false);
      if (target) {
        await focusFlaggedVessel(target);
      }
    } catch (error) {
      setSarUploadError(error instanceof Error ? error.message : "Unable to submit this SAR image.");
    } finally {
      setSarUploading(false);
    }
  };

  // Real model confidence for the selected forecast horizon (from the drift
  // engine's per-horizon confidence field); falls back to the legacy formula
  // only when no forecast rows exist yet.
  const forecastsList = incidentDetail?.forecasts || [];
  const activeForecast =
    horizon === 0 ? null : forecastsList.find((f: any) => Number(f.horizon_hours) === horizon);
  const modelConfidence = activeForecast?.confidence != null
    ? Math.round(Number(activeForecast.confidence) * 100)
    : null;

  const connectionLabel = { connected: "CONNECTED", connecting: "CONNECTING", reconnecting: "RECONNECTING", offline: "OFFLINE" }[connectionStatus];

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><div className="brand-mark"><span /></div><div><strong>AQUA SENTINEL</strong><small>MARITIME INTELLIGENCE NETWORK</small></div></div>
        <div className="header-center"><span className="live-dot" /> <span>LIVE OPERATIONS</span><i /> <span className="muted">{new Date().toUTCString()}</span></div>
        <div className="header-meta"><div><small>ACTIVE INCIDENTS</small><b>{activeCount < 10 ? `0${activeCount}` : activeCount}</b></div><div><small>VESSELS TRACKED</small><b>{vessels.length}</b></div><button className={`ais-fetch-btn ${aisFetching ? "busy" : ""}`} onClick={handleFetchAis} disabled={aisFetching} title="Trigger an immediate live-AIS poll; results appear in the live signal feed">{aisFetching ? "FETCHING…" : "⟳ FETCH AIS"}</button><button className="add-sar-btn" onClick={openSarUpload}>+ ADD SAR</button></div>
      </header>
      <main className="workspace">
        <section className="map-pane">
          <div className="map-frame"><div ref={mapNode} className="map-container" /></div>
          <div className="map-vignette" />
          <div className="map-label map-title"><small>OPERATIONAL PICTURE</small><h1>Konkan Coast / Sector 04</h1><span>Live vessel telemetry and fused SAR intelligence</span></div>
          <div className="map-legend"><small>LAYERS</small>{([['vessels', 'AIS vessels'], ['dark', 'Dark vessels'], ['protected', 'Protected zones']] as const).map(([key, label]) => <button key={key} className={`legend-item ${layers[key as keyof typeof layers] ? "active" : ""}`} onClick={() => setLayers((state) => ({ ...state, [key]: !state[key as keyof typeof layers] }))}><span className={`legend-swatch ${key}`} />{label}</button>)}</div>
          {selectedIncident && <div className="coordinate">{selectedIncident.coordinates[1].toFixed(2)}° N &nbsp; {selectedIncident.coordinates[0].toFixed(2)}° E</div>}
          {attributionMapLink && <div className="attribution-map-readout"><small>ATTRIBUTION SPATIAL CHECK</small><b>{attributionMapLink.distanceKm.toFixed(2)} km map separation</b><span>Spill centroid ↔ MMSI {attributionMapLink.mmsi}</span></div>}
          {(viewMode === "incident" || (viewMode === "vessel" && selectedVessel?.detected_spill_id)) && (
            <div className="forecast-bar"><div><small>FORECAST HORIZON</small><strong>{horizon === 0 ? "NOW" : `+${horizon}H`}</strong></div><div className="horizon-track">{[0, ...forecastsList.map((row: any) => Number(row.horizon_hours))].filter((value, index, values) => Number.isFinite(value) && values.indexOf(value) === index).map((value) => <button key={value} className={horizon === value ? "chosen" : ""} onClick={() => setHorizon(value)}><span>{value === 0 ? "Now" : `+${value}h`}</span></button>)}</div><div className="forecast-confidence"><small>MODEL CONFIDENCE{activeForecast?.model_version ? ` · ${String(activeForecast.model_version)}` : ""}</small><strong>{modelConfidence === null ? "Unavailable" : `${modelConfidence}%`}</strong></div></div>
          )}
        </section>
        <aside className="sidebar">
          <section className="side-section feed-section">
             <div className="section-head"><div><small>STREAM / 04</small><h2>Live signal feed</h2></div><span className={`connection ${connectionStatus}`}>{connectionLabel}</span></div>
             <div className="feed-list">
                {feed.map((item, idx) => <div className="feed-item" key={idx}><span className={`feed-icon ${item.kind}`}>{item.kind === "spill" ? "!" : item.kind === "risk" ? "↗" : item.kind === "dark" ? "◌" : "·"}</span><div><b>{item.title}</b><p>{item.body}</p></div><time>{item.time}</time></div>)}
             </div>
          </section>
          <div className="sidebar-bottom">
            <section className="side-section incidents-section">
               <div className="section-head"><div><small>MONITORED EVENTS</small><h2>Active incidents <em>{incidents.length < 10 ? `0${incidents.length}` : incidents.length}</em></h2></div><button className="text-button">View archive <span>→</span></button></div>
               <div className="incident-list">
                  {incidents.map((incident) => <button className={`incident-row ${selectedIncident?.id === incident.id ? "selected" : ""}`} key={incident.id} onClick={() => focusIncident(incident)}><span className={`severity-bar ${incident.severity}`} /><div className="incident-copy"><div><b>{incident.id}</b><span className={`severity-pill ${incident.severity}`}>{severityLabel[incident.severity]}</span></div><strong>{incident.title}</strong><p>{incident.location} <span>·</span> {incident.age}</p><small>Top attribution: <b>{incident.vessel}</b></small></div><span className="row-arrow">↗</span></button>)}
               </div>
            </section>
            
            <section className="side-section flagged-vessels-section">
               <div className="section-head"><div><small>RISK INTELLIGENCE</small><h2>Flagged Vessels <em>{vessels.filter(v => v.risk === "critical" || v.risk === "high").length}</em></h2></div></div>
               <div className="incident-list">
                  {vessels.filter(v => v.risk === "critical" || v.risk === "high").map((vessel) => (
                    <button className={`incident-row ${selectedVessel?.id === vessel.id && viewMode === "vessel" ? "selected" : ""}`} key={vessel.id} onClick={() => focusFlaggedVessel(vessel)}>
                      <span className={`severity-bar ${vessel.risk}`} />
                      <div className="incident-copy">
                        <div><b>{vessel.mmsi}</b><span className={`severity-pill ${vessel.risk}`}>{severityLabel[vessel.risk]}</span></div>
                        <strong>{vessel.name}</strong>
                        <p>{vessel.type} <span>·</span> Score: {vessel.score}/100</p>
                        <small>Verdict: <b>
                          {vessel.detected_spill_id ? <span style={{color: "#ed684c"}}>Oil Spill Detected</span> : 
                           vessel.sar_status === "fulfilled" ? <span style={{color: "#76bc99"}}>Clear (No Spill)</span> :
                           vessel.sar_status === "pending" ? <span style={{color: "#e5b75d"}}>SAR Pending</span> :
                           vessel.risk === "critical" ? "Tasking Requested" : "None"}
                        </b></small>
                      </div>
                      <span className="row-arrow">↗</span>
                    </button>
                  ))}
                </div>
             </section>

             <section className="side-section intel-section">
                <div className="section-head"><div><small>THREAT INTELLIGENCE</small><h2>STS &amp; Spoofing <em>{intelSts.length + intelSpoof.length}</em></h2></div></div>
                <div className="intel-list">
                   {intelSts.slice(0, 3).map((e: any, i: number) => (
                     <div className="intel-row" key={`sts-${i}`}>
                       <span className="intel-tag sts">STS</span>
                       <div><b>{String(e.vessel_a_mmsi)} ↔ {String(e.vessel_b_mmsi)}</b><p>{e.start_time ? new Date(e.start_time).toLocaleTimeString("en-US", { hour12: false, timeZone: "UTC" }) : ""} · {Number(e.duration_minutes || 0).toFixed(0)} min · {Number(e.confidence || 0).toFixed(2)} conf</p></div>
                     </div>
                   ))}
                   {intelSpoof.slice(0, 3).map((s: any, i: number) => (
                     <div className="intel-row" key={`spf-${i}`}>
                       <span className="intel-tag spoof">SPOOF</span>
                       <div><b>MMSI {s.mmsi}</b><p>trust {(s.rolling_trust_score ?? 1).toFixed?.(2) ?? s.rolling_trust_score} {s.flag ? `· flag ${s.flag}` : ""}{s.last_seen ? ` · seen ${new Date(s.last_seen).toLocaleTimeString("en-US", { hour12: false, timeZone: "UTC" })}` : ""}</p></div>
                     </div>
                   ))}
                   {intelSts.length === 0 && intelSpoof.length === 0 && (
                     <div className="intel-row empty"><div><b>No active STS or spoofing signals</b><p>Threat intelligence clear in the current window</p></div></div>
                   )}
                </div>
             </section>
           </div>
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
            onSpotVessel={spotVesselOnMap}
            forecastHorizon={horizon}
            onForecastHorizonChange={setHorizon}
         />
      )}

      {viewMode === "vessel" && (
         <VesselDetailsPage
            vesselDetail={vesselDetail}
            onBack={() => {
               setViewMode("map");
               setSelectedVessel(null);
               setVesselDetail(null);
            }}
            liveEvent={liveEvent}
         />
      )}
      
      {selectedVessel && viewMode !== "vessel" && (
      <div className="vessel-popover">
         <button onClick={() => setSelectedVessel(null)} aria-label="Close vessel details">×</button>
         <small>VESSEL PROFILE</small>
         <h3>{selectedVessel.name}</h3>
         <p>{selectedVessel.type} · MMSI {selectedVessel.mmsi}</p>
         <div className="vessel-risk">
             <span className={`severity-pill ${selectedVessel.risk}`}>{severityLabel[selectedVessel.risk]} risk</span>
             <strong>{selectedVessel.score}<small>/100</small></strong>
         </div>
         {selectedVessel.detected_spill_id ? (
             <div style={{ marginTop: "12px", color: "#ed684c", fontWeight: 600, fontSize: "11px", display: "flex", alignItems: "center", gap: "6px" }}>
                <span className="live-dot" style={{ background: "#ed684c" }} /> OIL SPILL DETECTED
             </div>
         ) : selectedVessel.sar_status === "fulfilled" ? (
             <div style={{ marginTop: "12px", color: "#76bc99", fontWeight: 600, fontSize: "11px", display: "flex", alignItems: "center", gap: "6px" }}>
                <span className="live-dot" style={{ background: "#76bc99", animation: "none" }} /> CLEAR (NO SPILL)
             </div>
         ) : selectedVessel.sar_status === "pending" || selectedVessel.risk === "critical" ? (
             <div style={{ marginTop: "12px", color: "#e5b75d", fontWeight: 600, fontSize: "11px", display: "flex", alignItems: "center", gap: "6px" }}>
                <span className="live-dot" style={{ background: "#e5b75d" }} /> SATELLITE TASKING REQUESTED
             </div>
         ) : null}
         {trackPoints.length >= 2 ? (
           <svg className="sparkline-svg" viewBox="0 0 120 32" preserveAspectRatio="none">
             <polyline
               fill="none" stroke="#7fd0e8" strokeWidth="1.5"
               points={(() => {
                 const speeds = trackPoints.map(p => p.speed).filter(s => s != null) as number[];
                 if (speeds.length < 2) return "";
                 const max = Math.max(...speeds), min = Math.min(...speeds);
                 const range = max - min || 1;
                 const pts = trackPoints.map((p, i) => {
                   const x = (i / (trackPoints.length - 1)) * 120;
                   const y = 30 - (((p.speed ?? min) - min) / range) * 28;
                   return `${x.toFixed(1)},${y.toFixed(1)}`;
                 }).join(" ");
                 return pts;
               })()}
             />
             <text x="2" y="10" fontSize="7" fill="#7fd0e8">{trackPoints.filter(p => p.speed != null).slice(-1)[0]?.speed?.toFixed(1) ?? ""} kn</text>
           </svg>
         ) : (
           <div className="sparkline"><i /><i /><i /><i /><i /><i /><i /><i /></div>
         )}
          <p className="vessel-detail">{selectedVessel.detail}</p>
      </div>
      )}

      <SARTaskingPipeline liveEvent={liveEvent} historicalSceneId={historicalSceneId} tasking={vesselDetail?.sar_tasking} />

      {sarUploadOpen && (
        <div className="sar-upload-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !sarUploading) setSarUploadOpen(false); }}>
          <form className="sar-upload-dialog" onSubmit={handleSarUpload}>
            <div className="sar-upload-title">
              <div><small>MANUAL INGESTION</small><h2>Add SAR Image</h2></div>
              <button type="button" aria-label="Close SAR upload" onClick={() => setSarUploadOpen(false)} disabled={sarUploading}>×</button>
            </div>
            <p>The image will run through despeckling, CFAR detection, morphological cleaning, polygon extraction, evidence fusion, severity, attribution, forecasting, and response recommendations.</p>
            <label>
              <span>ASSOCIATE WITH VESSEL</span>
              <select value={sarUploadMmsi} onChange={(event) => setSarUploadMmsi(event.target.value)} required>
                <option value="" disabled>Select a tracked vessel</option>
                {vessels.map((vessel) => <option key={vessel.mmsi} value={vessel.mmsi}>{vessel.name} · MMSI {vessel.mmsi}</option>)}
              </select>
            </label>
            <label>
              <span>SAR RASTER</span>
              <input type="file" accept=".tif,.tiff,.png,.jpg,.jpeg,image/tiff,image/png,image/jpeg" onChange={(event) => setSarUploadFile(event.target.files?.[0] || null)} required />
              <small>GeoTIFF (EPSG:4326) preserves embedded coordinates. PNG/JPEG is centred on the selected vessel at 10 m/pixel. Maximum 50 MB.</small>
            </label>
            {sarUploadError && <div className="sar-upload-error" role="alert">{sarUploadError}</div>}
            <div className="sar-upload-actions">
              <button type="button" onClick={() => setSarUploadOpen(false)} disabled={sarUploading}>CANCEL</button>
              <button type="submit" disabled={!sarUploadMmsi || !sarUploadFile || sarUploading}>{sarUploading ? "SUBMITTING…" : "RUN SAR PIPELINE"}</button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}

export default App;
