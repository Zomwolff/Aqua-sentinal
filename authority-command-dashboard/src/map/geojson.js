export const EMPTY_COLLECTION = { type: "FeatureCollection", features: [] };

export function validCoordinate(value) {
  return Array.isArray(value) && value.length >= 2 && value[0] !== null && value[1] !== null && value[0] !== "" && value[1] !== "" && Number.isFinite(Number(value[0])) && Number.isFinite(Number(value[1]));
}

export function isUsableGeometry(geometry) {
  if (!geometry || !["Point", "Polygon", "MultiPolygon", "LineString", "MultiLineString"].includes(geometry.type)) return false;
  return Array.isArray(geometry.coordinates);
}

export function feature(geometry, properties = {}, id) {
  return isUsableGeometry(geometry) ? { type: "Feature", id, properties, geometry } : null;
}

export function collection(features) {
  return { type: "FeatureCollection", features: features.filter(Boolean) };
}

export function point(lon, lat, properties = {}, id) {
  return validCoordinate([lon, lat]) ? feature({ type: "Point", coordinates: [Number(lon), Number(lat)] }, properties, id) : null;
}

export function boundsForGeometry(geometry, bounds = [Infinity, Infinity, -Infinity, -Infinity]) {
  if (!isUsableGeometry(geometry)) return null;
  const visit = (coords) => {
    if (typeof coords[0] === "number") {
      if (Number.isFinite(coords[0]) && Number.isFinite(coords[1])) { bounds[0] = Math.min(bounds[0], coords[0]); bounds[1] = Math.min(bounds[1], coords[1]); bounds[2] = Math.max(bounds[2], coords[0]); bounds[3] = Math.max(bounds[3], coords[1]); }
      return;
    }
    coords.forEach(visit);
  };
  visit(geometry.coordinates);
  return Number.isFinite(bounds[0]) ? bounds : null;
}
