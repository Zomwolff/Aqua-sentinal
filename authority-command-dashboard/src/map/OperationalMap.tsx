import { useEffect, useRef } from "react";
import maplibregl, { Map as MapLibreMap } from "maplibre-gl";
import type { DarkVessel, IncidentDetail, ProtectedArea, Vessel } from "../types";
import { boundsForGeometry, collection, EMPTY_COLLECTION, feature, point } from "./geojson";
import { severityClass } from "../utils/format";

type Layers = { vessels: boolean; spill: boolean; dark: boolean; zones: boolean; forecast: boolean; coastline: boolean };
type Props = { detail: IncidentDetail | null; vessels: Vessel[]; darkVessels: DarkVessel[]; areas: ProtectedArea[]; layers: Layers; horizon: number; onVesselClick: (mmsi: string) => void; };

const visible = (value: boolean) => value ? "visible" : "none";
const empty = () => EMPTY_COLLECTION as any;

export function OperationalMap({ detail, vessels, darkVessels, areas, layers, horizon, onVesselClick }: Props) {
  const node = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const loaded = useRef(false);

  useEffect(() => {
    if (!node.current) return;
    const map = new maplibregl.Map({
      container: node.current,
      center: [72.8, 19.0], zoom: 6,
      style: { version: 8, sources: { osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, attribution: "© OpenStreetMap contributors" } }, layers: [{ id: "sea", type: "background", paint: { "background-color": "#071b25" } }, { id: "base", type: "raster", source: "osm", paint: { "raster-opacity": .55, "raster-saturation": -.8, "raster-brightness-max": .55 } }] },
      attributionControl: false,
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: true }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "nautical", maxWidth: 100 }), "bottom-left");
    mapRef.current = map;
    map.on("load", () => {
      const add = (name: string) => map.addSource(name, { type: "geojson", data: empty() });
      ["spill", "forecast", "vessels", "dark", "zones", "source"].forEach(add);
      map.addLayer({ id: "zones-fill", type: "fill", source: "zones", paint: { "fill-color": "#43b6a5", "fill-opacity": .13 } });
      map.addLayer({ id: "zones-line", type: "line", source: "zones", paint: { "line-color": "#63cdbc", "line-width": 1.3, "line-dasharray": [3, 2] } });
      map.addLayer({ id: "forecast-fill", type: "fill", source: "forecast", paint: { "fill-color": "#4eb8df", "fill-opacity": .22 } });
      map.addLayer({ id: "forecast-line", type: "line", source: "forecast", paint: { "line-color": "#79d7ef", "line-width": 2, "line-dasharray": [2, 2] } });
      map.addLayer({ id: "spill-fill", type: "fill", source: "spill", paint: { "fill-color": "#ef625a", "fill-opacity": .34 } });
      map.addLayer({ id: "spill-line", type: "line", source: "spill", paint: { "line-color": "#ff8c75", "line-width": 2.5 } });
      map.addLayer({ id: "vessel-symbol", type: "symbol", source: "vessels", layout: { "text-field": "▲", "text-size": 17, "text-rotate": ["coalesce", ["get", "heading"], 0], "text-rotation-alignment": "map", "text-allow-overlap": true }, paint: { "text-color": ["match", ["get", "risk"], "critical", "#ff5f59", "high", "#ff9a46", "medium", "#f5c451", "#80d1ac"], "text-halo-color": "#071214", "text-halo-width": 1.5 } });
      map.addLayer({ id: "dark-halo", type: "circle", source: "dark", paint: { "circle-radius": 13, "circle-color": "#d64d72", "circle-opacity": .14, "circle-stroke-color": "#ef6685", "circle-stroke-width": 1 } });
      map.addLayer({ id: "dark-point", type: "circle", source: "dark", paint: { "circle-radius": 4, "circle-color": "#131821", "circle-stroke-color": "#ff7b9b", "circle-stroke-width": 2 } });
      map.addLayer({ id: "source-point", type: "circle", source: "source", paint: { "circle-radius": 8, "circle-color": "#f4c75c", "circle-stroke-color": "#fff3c2", "circle-stroke-width": 1.5 } });
      map.on("click", "vessel-symbol", event => { const props = event.features?.[0]?.properties || {}; const mmsi = props.mmsi; if (mmsi) onVesselClick(String(mmsi)); new maplibregl.Popup({ closeButton: false }).setLngLat(event.lngLat).setText(`${props.name || "Unnamed vessel"}\nMMSI: ${props.mmsi || "—"}\nRisk: ${String(props.risk || "unknown").toUpperCase()}`).addTo(map); });
      map.on("click", "spill-fill", event => { const props = event.features?.[0]?.properties || {}; new maplibregl.Popup({ closeButton: false }).setLngLat(event.lngLat).setText(`SPILL\nIncident: ${props.incident || "—"}`).addTo(map); });
      map.on("click", "dark-point", event => { const props = event.features?.[0]?.properties || {}; new maplibregl.Popup({ closeButton: false }).setLngLat(event.lngLat).setText(`DARK VESSEL\nDetected: ${props.detected_at || "—"}\nConfidence: ${props.confidence === "" ? "—" : props.confidence}`).addTo(map); });
      map.on("mouseenter", "vessel-symbol", () => map.getCanvas().style.cursor = "pointer");
      map.on("mouseleave", "vessel-symbol", () => map.getCanvas().style.cursor = "");
      loaded.current = true;
    });
    return () => { loaded.current = false; map.remove(); mapRef.current = null; };
  }, [onVesselClick]);

  useEffect(() => {
    const map = mapRef.current; if (!map || !loaded.current) return;
    const set = (name: string, data: any) => (map.getSource(name) as maplibregl.GeoJSONSource)?.setData(data);
    set("vessels", collection(vessels.map(v => point(v.last_lon, v.last_lat, { mmsi: v.mmsi, name: v.vessel_name || "Unnamed vessel", risk: severityClass(v.risk_tier), heading: v.heading_deg || 0 }, v.mmsi))));
    set("dark", collection(darkVessels.map(v => point(v.longitude, v.latitude, { mmsi: v.matched_mmsi || "", detected_at: v.detected_at || "", confidence: v.confidence ?? "" }, v.id))));
    set("zones", collection(areas.map(a => feature(a.geometry, { name: a.name || "Reference zone", type: a.type || "" }, a.id))));
    set("spill", detail ? collection([feature(detail.incident.geometry, { incident: detail.incident.id })]) : empty());
    const forecast = detail?.forecasts.find(f => Number(f.horizon_hours) === horizon);
    set("forecast", forecast ? collection([feature(forecast.geometry, { horizon })]) : empty());
    const source = detail?.attribution[0];
    set("source", source ? collection([point(source.last_lon, source.last_lat, { mmsi: source.mmsi || "", name: source.vessel_name || "Probable source" })]) : empty());
    const layouts: [string, boolean][] = [["vessel-symbol", layers.vessels], ["dark-halo", layers.dark], ["dark-point", layers.dark], ["zones-fill", layers.zones], ["zones-line", layers.zones], ["spill-fill", layers.spill], ["spill-line", layers.spill], ["forecast-fill", layers.forecast], ["forecast-line", layers.forecast], ["source-point", layers.vessels]];
    layouts.forEach(([id, on]) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", visible(on)));
    map.setLayoutProperty("base", "visibility", visible(layers.coastline));
  }, [detail, vessels, darkVessels, areas, layers, horizon]);

  useEffect(() => {
    const map = mapRef.current; if (!map || !detail || !loaded.current) return;
    const bounds = boundsForGeometry(detail.incident.geometry);
    if (bounds) map.fitBounds(bounds as [number, number, number, number], { padding: 80, maxZoom: 12, duration: 650 });
    else if (Number.isFinite(Number(detail.incident.longitude)) && Number.isFinite(Number(detail.incident.latitude))) map.flyTo({ center: [Number(detail.incident.longitude), Number(detail.incident.latitude)], zoom: 10, duration: 650 });
  }, [detail?.incident.id]);
  return <div className="map-canvas" ref={node} />;
}
