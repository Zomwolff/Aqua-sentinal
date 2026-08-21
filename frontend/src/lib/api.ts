export const API_BASE = `http://${window.location.hostname}:8015`;

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
