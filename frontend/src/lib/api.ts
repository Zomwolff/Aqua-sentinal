// All requests go through the nginx reverse proxy on the dashboard origin
// (/api -> api-gateway:8000, /artifacts -> api-gateway:8000/artifacts),
// so the UI works identically behind proxies without hardcoded ports.
// In dev mode, vite.config.ts mirrors the same proxy rules.
export const API_BASE = "/api";
export const ARTIFACTS_BASE = "/artifacts";

const SCENE_DIRECTORY_UNSAFE = /[^A-Za-z0-9_.=-]/g;

/**
 * Match services/shared/artifacts.py, which converts a scene ID to a safe
 * artifact-directory name before writing it to the shared volume.
 */
export function artifactUrl(sceneId: string, filename: string): string {
  const sceneDirectory = String(sceneId).replace(SCENE_DIRECTORY_UNSAFE, "_");
  return `${ARTIFACTS_BASE}/${encodeURIComponent(sceneDirectory)}/${encodeURIComponent(filename)}`;
}

export const SAR_ARTIFACT_FILENAMES = {
  raw: "raw_image.png",
  filtered: "filtered_image.png",
  cfar: "candidate_mask.png",
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

export async function fetchSpillCandidate(id: string) {
  const res = await fetch(`${API_BASE}/spill/candidates/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error("Failed to fetch spill candidate");
  return res.json();
}

export async function fetchVesselDetail(mmsi: string | number) {
  const res = await fetch(`${API_BASE}/vessels/${mmsi}`);
  if (!res.ok) throw new Error("Failed to fetch vessel detail");
  return res.json();
}

export async function fetchFlaggedVesselDetail(mmsi: string | number) {
  const res = await fetch(`${API_BASE}/vessels/${encodeURIComponent(String(mmsi))}/detail`);
  if (!res.ok) throw new Error("Failed to fetch flagged vessel detail");
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

export async function uploadSarImage(mmsi: string | number, image: File) {
  const form = new FormData();
  form.append("image", image);
  const res = await fetch(`${API_BASE}/sar/upload/${encodeURIComponent(String(mmsi))}`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const payload = await res.json().catch(() => null);
    throw new Error(payload?.detail || "Failed to submit the SAR image");
  }
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
