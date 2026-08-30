import { useEffect, useState } from "react";

import { getSarSceneMetadata } from "../lib/apiClient";

function formatDegrees(value, positiveSuffix, negativeSuffix) {
  const suffix = value >= 0 ? positiveSuffix : negativeSuffix;
  return `${Math.abs(value).toFixed(2)}°${suffix}`;
}

/**
 * "SAR Image Information" panel (GeoTIFF support, requirement 7).
 *
 * Every value shown is read live from the sar-spill-intelligence
 * /scenes/{scene_id}/metadata endpoint — nothing here is inferred or
 * hardcoded, so it reflects the actual uploaded/processed raster (format,
 * CRS, resolution, dimensions, bounds, bands) rather than assumed defaults.
 */
export function SARImageInfoPanel({ sceneId }) {
  const [metadata, setMetadata] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setMetadata(null);
    setError(null);
    if (!sceneId) return;

    let cancelled = false;
    (async () => {
      try {
        const data = await getSarSceneMetadata(sceneId);
        if (!cancelled) setMetadata(data);
      } catch (err) {
        if (!cancelled) setError(String(err));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [sceneId]);

  if (!sceneId) return null;

  return (
    <div className="panel sar-image-info-panel">
      <h2>SAR Image</h2>
      {error && <p className="sar-image-info-error">Metadata unavailable for this scene.</p>}
      {!error && !metadata && <p>Loading…</p>}
      {metadata && (
        <dl>
          <dt>Format</dt>
          <dd>{metadata.format}</dd>

          <dt>CRS</dt>
          <dd>{metadata.epsg ? `EPSG:${metadata.epsg}` : metadata.crs || "Unknown"}</dd>

          <dt>Resolution</dt>
          <dd>{metadata.resolution_m} m</dd>

          <dt>Width</dt>
          <dd>{metadata.width} px</dd>

          <dt>Height</dt>
          <dd>{metadata.height} px</dd>

          <dt>Bounds</dt>
          <dd>
            {formatDegrees(metadata.bounds.south, "N", "S")} – {formatDegrees(metadata.bounds.north, "N", "S")}
            <br />
            {formatDegrees(metadata.bounds.west, "E", "W")} – {formatDegrees(metadata.bounds.east, "E", "W")}
          </dd>

          <dt>Bands</dt>
          <dd>{metadata.band_labels && metadata.band_labels.length ? metadata.band_labels.join(", ") : metadata.bands}</dd>
        </dl>
      )}
    </div>
  );
}
