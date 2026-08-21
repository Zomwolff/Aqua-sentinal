import test from "node:test";
import assert from "node:assert/strict";
import { boundsForGeometry, collection, point } from "../src/map/geojson.js";
import { percent, reconnectDelay, selectedForecast, severity, severityClass } from "../src/utils/format.js";

test("severity display uses backend tiers without fabricating a value", () => {
  assert.equal(severity("MODERATE"), "MEDIUM");
  assert.equal(severityClass("CRITICAL"), "critical");
  assert.equal(percent(null), "—");
  assert.equal(percent(0.87), "87%");
});

test("geometry conversion accepts real GeoJSON coordinate order and rejects missing positions", () => {
  assert.deepEqual(point(72.8, 19.1)?.geometry.coordinates, [72.8, 19.1]);
  assert.equal(point(null, 19.1), null);
  const polygon = { type: "Polygon", coordinates: [[[72, 18], [73, 18], [73, 19], [72, 18]]] };
  assert.deepEqual(boundsForGeometry(polygon), [72, 18, 73, 19]);
  assert.equal(collection([null, point(1, 2)]).features.length, 1);
});

test("forecast timeline only selects an available server-provided horizon", () => {
  const forecasts = [{ horizon_hours: 6 }, { horizon_hours: 24 }];
  assert.equal(selectedForecast(forecasts, 24)?.horizon_hours, 24);
  assert.equal(selectedForecast(forecasts, 48), null);
});

test("websocket reconnect backoff is bounded", () => {
  assert.equal(reconnectDelay(0), 1000);
  assert.equal(reconnectDelay(3), 8000);
  assert.equal(reconnectDelay(10), 30000);
});
