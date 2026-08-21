import { useState } from "react";
import { MapContainer, TileLayer } from "react-leaflet";

import { useLiveEvents } from "./hooks/useLiveEvents";
import { SpillCandidateLayer } from "./components/SpillCandidateLayer";
import { VesselLayer } from "./components/VesselLayer";
import { DriftForecastLayer } from "./components/DriftForecastLayer";
import { AlertFeed } from "./components/AlertFeed";
import { IncidentList } from "./components/IncidentList";
import { IncidentDetailPanel } from "./components/IncidentDetailPanel";

export default function App() {
  const [liveEvent, setLiveEvent] = useState(null);
  const [selectedSpillId, setSelectedSpillId] = useState(null);

  useLiveEvents({ onEvent: setLiveEvent });

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
        </aside>
      </main>
    </div>
  );
}