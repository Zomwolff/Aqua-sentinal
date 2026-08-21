/**
 * Public API origin. In Docker/local development the browser reaches the
 * gateway through the same host as the dashboard on port 8015. Deployments
 * can override that with VITE_API_URL at build time.
 */
const configuredApiBase = import.meta.env.VITE_API_URL?.trim();

export const API_BASE = (configuredApiBase || `http://${window.location.hostname}:8015`)
  .replace(/\/+$/, "");

const SCENE_DIRECTORY_UNSAFE = /[^A-Za-z0-9_.=-]/g;

/**
 * Match services/shared/artifacts.py, which converts a scene ID to a safe
 * artifact-directory name before writing it to the shared volume.
 */
export function artifactUrl(sceneId: string, filename: string): string {
  const sceneDirectory = String(sceneId).replace(SCENE_DIRECTORY_UNSAFE, "_");
  return `${API_BASE}/artifacts/${encodeURIComponent(sceneDirectory)}/${encodeURIComponent(filename)}`;
}

export const SAR_ARTIFACT_FILENAMES = {
  raw: "raw_image.png",
  filtered: "filtered_image.png",
  cfar: "bright_target_mask.png",
  final: "cleaned_mask.png",
} as const;

export type SarArtifactKey = keyof typeof SAR_ARTIFACT_FILENAMES;

export function sarArtifactUrls(sceneId: string): Record<SarArtifactKey, string> {
  return Object.fromEntries(
    Object.entries(SAR_ARTIFACT_FILENAMES).map(([key, filename]) => [
      key,
      artifactUrl(sceneId, filename),
    ]),
  ) as Record<SarArtifactKey, string>;
}

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
