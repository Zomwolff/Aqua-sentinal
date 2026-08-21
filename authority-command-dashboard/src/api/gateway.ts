import type { DarkVessel, IncidentDetail, IncidentSummary, ProtectedArea, Vessel } from "../types";

export const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";

async function request<T>(path: string, init?: RequestInit, fetcher: typeof fetch = fetch): Promise<T> {
  const response = await fetcher(`${API_BASE}${path}`, init);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json() as Promise<T>;
}

export async function getIncidents(fetcher?: typeof fetch) {
  return request<{ count: number; incidents: IncidentSummary[] }>("/spill/incidents?since_hours=168", undefined, fetcher);
}
export async function getIncidentFull(id: string, fetcher?: typeof fetch) { return request<IncidentDetail>(`/spill/incidents/${encodeURIComponent(id)}`, undefined, fetcher); }
export async function getVessels(fetcher?: typeof fetch) { return request<{ total: number; vessels: Vessel[] }>("/vessels?active_since_hours=168&limit=500", undefined, fetcher); }
export async function getVesselFull(mmsi: string, fetcher?: typeof fetch) { return request(`/vessels/${encodeURIComponent(mmsi)}`, undefined, fetcher); }
export async function getDarkVessels(fetcher?: typeof fetch) { return request<{ count: number; dark_vessels: DarkVessel[] }>("/dark-vessels?since_hours=168", undefined, fetcher); }
export async function getReferenceLayers(fetcher?: typeof fetch) { return request<{ count: number; protected_areas: ProtectedArea[] }>("/protected-areas", undefined, fetcher); }
export async function acknowledgeRecommendation(id: number, operator = "authority-operator", fetcher?: typeof fetch) {
  return request(`/spill/recommendations/${id}/acknowledge?acknowledged_by=${encodeURIComponent(operator)}`, { method: "PATCH" }, fetcher);
}

export function liveUrl(): string {
  const url = new URL("/live", window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}
