/** Minimal api-gateway client for map layers (spill candidates + vessels). */

export function apiBase() {
  if (import.meta.env && import.meta.env.VITE_API_URL) {
    return import.meta.env.VITE_API_URL.replace(/\/$/, "");
  }
  const port = window.location.port === "3000" ? "8015" : window.location.port;
  return `${window.location.protocol}//${window.location.hostname}:${port}`;
}

async function json(fetcher, path) {
  const response = await fetcher(`${apiBase()}${path}`);
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`GET ${path} failed: ${response.status} ${body}`);
  }
  return response.json();
}

/** Full spill candidate details (geometry + metadata) by candidate_id. */
export async function getSpillCandidate(candidateId, fetcher = fetch) {
  return json(fetcher, `/spill/candidates/${encodeURIComponent(candidateId)}`);
}

/** Vessel details (incl. last position) by mmsi — used for the AIS vessel layer. */
export async function getVessel(mmsi, fetcher = fetch) {
  return json(fetcher, `/vessels/${encodeURIComponent(mmsi)}`);
}
export async function getSpillIncidents(fetcher = fetch) {
  return json(fetcher, `/spill/incidents`);
}

export async function getSpillIncident(spillId, fetcher = fetch) {
  return json(fetcher, `/spill/incidents/${encodeURIComponent(spillId)}`);
}

export async function getSpillAttribution(spillId, fetcher = fetch) {
  return json(fetcher, `/spill/incidents/${encodeURIComponent(spillId)}/attribution`);
}

export async function getSpillForecast(spillId, fetcher = fetch) {
  return json(fetcher, `/spill/incidents/${encodeURIComponent(spillId)}/forecast`);
}

export async function getSpillSeverity(spillId, fetcher = fetch) {
  return json(fetcher, `/spill/incidents/${encodeURIComponent(spillId)}/severity`);
}

export async function getSpillRecommendations(spillId, fetcher = fetch) {
  return json(fetcher, `/spill/incidents/${encodeURIComponent(spillId)}/recommendations`);
}

export async function getCostProjection(spillId, fetcher = fetch) {
  return json(fetcher, `/cost-projection/${encodeURIComponent(spillId)}`);
}

/** SAR raster metadata (CRS, bounds, resolution, bands, ...) for a scene_id. */
export async function getSarSceneMetadata(sceneId, fetcher = fetch) {
  return json(fetcher, `/sar/scenes/${encodeURIComponent(sceneId)}/metadata`);
}

/** Public URL for a scene's processed SAR raster preview PNG. */
export function sarArtifactPreviewUrl(sceneId, artifact = "filtered_image.png") {
  return `${apiBase()}/artifacts/${encodeURIComponent(sceneId)}/${artifact}`;
}
