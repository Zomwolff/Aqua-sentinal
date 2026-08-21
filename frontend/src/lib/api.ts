// All requests go through the nginx reverse proxy on the dashboard origin
// (/api -> api-gateway:8000, /artifacts -> api-gateway:8000/artifacts),
// so the UI works identically behind proxies without hardcoded ports.
// In dev mode, vite.config.ts mirrors the same proxy rules.
export const API_BASE = "/api";
export const ARTIFACTS_BASE = "/artifacts";

export async function fetchVessels() {
  const res = await fetch(`${API_BASE}/vessels?active_since_hours=168`);
  if (!res.ok) throw new Error("Failed to fetch vessels");
  return res.json();
}

export async function fetchIncidents() {
  const res = await fetch(`${API_BASE}/spill/incidents?since_hours=168`);
  if (!res.ok) throw new Error("Failed to fetch incidents");
  return res.json();
}

export async function fetchIncidentDetail(id: string) {
  const res = await fetch(`${API_BASE}/spill/incidents/${id}`);
  if (!res.ok) throw new Error("Failed to fetch incident detail");
  return res.json();
}

export async function fetchVesselDetail(mmsi: string | number) {
  const res = await fetch(`${API_BASE}/vessels/${mmsi}`);
  if (!res.ok) throw new Error("Failed to fetch vessel detail");
  return res.json();
}

export async function fetchVesselTrack(mmsi: string | number, hours = 24) {
  const res = await fetch(`${API_BASE}/vessels/${mmsi}/track?hours=${hours}`);
  if (!res.ok) throw new Error("Failed to fetch vessel track");
  return res.json();
}

/** Ask the ais-reader service for an immediate live-AIS poll. */
export async function triggerLiveAisFetch() {
  const res = await fetch(`${API_BASE}/ingest/ais/fetch-now`, { method: "POST" });
  if (!res.ok) throw new Error("Failed to trigger AIS fetch");
  return res.json();
}

export async function fetchProtectedAreas() {
  const res = await fetch(`${API_BASE}/protected-areas`);
  if (!res.ok) throw new Error("Failed to fetch protected areas");
  return res.json();
}

export async function fetchDarkVessels(sinceHours = 24) {
  const res = await fetch(`${API_BASE}/dark-vessels?since_hours=${sinceHours}`);
  if (!res.ok) throw new Error("Failed to fetch dark vessels");
  return res.json();
}

export async function fetchStsEvents(sinceHours = 48) {
  const res = await fetch(`${API_BASE}/sts?since_hours=${sinceHours}`);
  if (!res.ok) throw new Error("Failed to fetch STS events");
  return res.json();
}

export async function fetchSpoofingSuspects(trustThreshold = 0.5) {
  const res = await fetch(`${API_BASE}/spoofing/suspects?trust_threshold=${trustThreshold}`);
  if (!res.ok) throw new Error("Failed to fetch spoofing suspects");
  return res.json();
}
