import { useState } from "react";
import { MapContainer, TileLayer } from "react-leaflet";

import { useLiveEvents } from "./hooks/useLiveEvents";
import { SpillCandidateLayer } from "./components/SpillCandidateLayer";
import { VesselLayer } from "./components/VesselLayer";

export default function App() {
  const [liveEvent, setLiveEvent] = useState(null);
  useLiveEvents({ onEvent: setLiveEvent });

  return (
    <div className="app">
      <header className="app-header">
        <h1>Aqua Sentinel</h1>
        <span>Maritime Oil-Spill Detection &amp; Attribution</span>
      </header>
      <main className="app-main">
        <section className="map-container">
          <MapContainer
            center={[15.0, 73.0]}
            zoom={7}
            style={{ height: "100%", width: "100%" }}
          >
            <TileLayer url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
            <VesselLayer event={liveEvent} />
            <SpillCandidateLayer event={liveEvent} />
          </MapContainer>
        </section>
        <aside className="sidebar">
          <div className="panel">
            <h2>Alert Feed</h2>
          </div>
          <div className="panel">
            <h2>Incident List</h2>
          </div>
        </aside>
      </main>
    </div>
  );
}