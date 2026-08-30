import { useEffect, useState } from "react";
import { MapContainer, TileLayer } from "react-leaflet";

import { useLiveEvents } from "./hooks/useLiveEvents";
import { getSpillCandidate } from "./lib/apiClient";
import { SpillCandidateLayer } from "./components/SpillCandidateLayer";
import { VesselLayer } from "./components/VesselLayer";
import { DriftForecastLayer } from "./components/DriftForecastLayer";
import { SARRasterLayer } from "./components/SARRasterLayer";
import { SARImageInfoPanel } from "./components/SARImageInfoPanel";
import { AlertFeed } from "./components/AlertFeed";
import { IncidentList } from "./components/IncidentList";
import { IncidentDetailPanel } from "./components/IncidentDetailPanel";

export default function App() {
  const [liveEvent, setLiveEvent] = useState(null);
  const [selectedSpillId, setSelectedSpillId] = useState(null);
  const [selectedSceneId, setSelectedSceneId] = useState(null);

  useLiveEvents({ onEvent: setLiveEvent });

  // The map/candidate layers key off candidate_id; the SAR raster overlay
  // and info panel key off scene_id (one scene can have several candidates),
  // so resolve it once a spill is selected rather than threading scene_id
  // through every event.
  useEffect(() => {
    if (!selectedSpillId) {
      setSelectedSceneId(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const candidate = await getSpillCandidate(selectedSpillId);
        if (!cancelled) setSelectedSceneId(candidate.scene_id || null);
      } catch (error) {
        if (!cancelled) setSelectedSceneId(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedSpillId]);

  return (
    <div className="app">
      <header className="app-header">
        <h1>Aqua Sentinel</h1>
        <span>Maritime Oil-Spill Detection &amp; Attribution</span>
      </header>
      <main className="app-main">
        <section className="map-container" style={{ position: "relative" }}>
          <MapContainer
            center={[15.0, 73.0]}
            zoom={7}
            style={{ height: "100%", width: "100%" }}
          >
            <TileLayer url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
            <SARRasterLayer sceneId={selectedSceneId} />
            <VesselLayer event={liveEvent} />
            <SpillCandidateLayer event={liveEvent} onSpillSelect={setSelectedSpillId} />
            <DriftForecastLayer selectedSpillId={selectedSpillId} />
          </MapContainer>
          
          <IncidentDetailPanel 
            spillId={selectedSpillId} 
            onClose={() => setSelectedSpillId(null)} 
          />
        </section>
        <aside className="sidebar">
          <div className="panel">
            <h2>Alert Feed</h2>
            <AlertFeed event={liveEvent} />
          </div>
          <div className="panel">
            <h2>Incident List</h2>
            <IncidentList onIncidentSelect={setSelectedSpillId} />
          </div>
          <SARImageInfoPanel sceneId={selectedSceneId} />
        </aside>
      </main>
    </div>
  );
}