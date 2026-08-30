import { useEffect, useState } from "react";
import { ImageOverlay } from "react-leaflet";

import { getSarSceneMetadata, sarArtifactPreviewUrl } from "../lib/apiClient";

/**
 * Georeferenced SAR raster overlay for a single scene (Step: GeoTIFF support,
 * requirement 4 — "Allow GeoTIFF-derived SAR imagery to be displayed on the
 * map... correctly positioned").
 *
 * Renders the scene's despeckled preview PNG (produced by
 * shared/artifacts.py's save_scene_artifact) as a Leaflet ImageOverlay,
 * positioned using the *actual* bounds of the normalised (always-EPSG:4326)
 * GeoTIFF — never assumed from the image's pixel dimensions. Bounds come
 * from the sar-spill-intelligence /scenes/{scene_id}/metadata endpoint, so a
 * reprojected (originally non-4326) scene still aligns correctly with the
 * base map and with the SpillCandidateLayer/VesselLayer polygons, which are
 * already in EPSG:4326.
 *
 * Renders nothing when `sceneId` is falsy, when the scene has no processed
 * raster yet, or while metadata is loading — callers don't need to guard.
 */
export function SARRasterLayer({ sceneId, opacity = 0.7 }) {
  const [metadata, setMetadata] = useState(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setMetadata(null);
    setFailed(false);
    if (!sceneId) return;

    let cancelled = false;
    (async () => {
      try {
        const data = await getSarSceneMetadata(sceneId);
        if (!cancelled) setMetadata(data);
      } catch (error) {
        if (!cancelled) setFailed(true);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [sceneId]);

  if (!sceneId || failed || !metadata) return null;

  const { west, south, east, north } = metadata.bounds;
  // Leaflet bounds are [[lat, lat], [lng, lng]] i.e. [[south, west], [north, east]].
  const bounds = [
    [south, west],
    [north, east],
  ];

  return (
    <ImageOverlay
      url={sarArtifactPreviewUrl(sceneId, "filtered_image.png")}
      bounds={bounds}
      opacity={opacity}
    />
  );
}
