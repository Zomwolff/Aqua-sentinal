# API Route Design — Every Service, Every Route

## How to think about routes in this system

Every service in this architecture is **primarily event-driven** — the real work happens in the background worker consuming Redis Streams, not in an HTTP request. But every service still needs a REST surface for three reasons:

1. **Health/readiness** — docker-compose, and you during debugging, need to know a container is alive and its worker loop is actually running (not just that the process started).
2. **Read access** — the Gateway (and you, manually, with curl/Postman while building) needs to query "what does this service currently know" without going through the stream.
3. **Manual trigger/debug** — during a 3-day build, you will constantly want to say "process this one record right now" without waiting for the simulator to get there in its playback — every service should expose a manual trigger route for exactly this.

So every service gets the same three route *categories*, and then a small number of service-specific read routes on top. Learn this pattern once; below, don't re-explain it every time — only the parts that differ per service are called out.

**Universal routes every service has (not repeated per service below):**
- `GET /health` → `{status: "ok", worker_alive: true/false, last_message_processed_at: timestamp}`. Purpose: container orchestration and your own sanity-checking during integration. `worker_alive` should reflect whether the background consumer loop is actually still running (e.g., a heartbeat timestamp updated each loop iteration, checked against a staleness threshold) — not just that the HTTP server is up, since those can silently diverge (HTTP fine, worker crashed).
- `GET /metrics` (optional, nice-to-have) → basic counters: messages processed, messages failed, average processing latency. Purpose: lets you show judges a "system health" view on the dashboard if you have time, and is genuinely useful for catching a service silently falling behind during your own demo rehearsal.

---

## MODULE A — Data Pipeline & Vessel Intelligence

### A1. Data Ingestion Service

- **`POST /ingest/ais`**
  Body: a single AIS record or batch array `{mmsi, lat, lon, speed_knots, course, heading, timestamp, vessel_type, vessel_name}[]`.
  What it does: this is the actual entry point the simulator calls (rather than the simulator writing to Redis directly) — keeping ingestion behind an HTTP route means the same validation/cleaning code path is used whether data comes from the simulator, a manual curl during testing, or (in a real deployment) an actual AIS provider webhook. Runs the record(s) through validation → dedup → time sync, writes to `ais.clean`, returns `{accepted: n, rejected: n, rejection_reasons: [...]}`.

- **`POST /ingest/sar`** and **`POST /ingest/optical`**
  Body: `{tile_id, sensor, capture_timestamp, bbox, file_path, polarization}` (the image file itself lives on a shared volume/`/data` mount — the route just registers metadata and triggers georeferencing, it does not upload binary imagery over HTTP).
  What it does: validates the tile metadata, confirms the file exists and opens cleanly with `rasterio`, attaches/confirms CRS, publishes to `sar.clean`/`optical.clean`.

- **`POST /ingest/env`**
  Body: `{lat, lon, timestamp, wind_speed, wind_direction, current_speed, current_direction}[]`.
  What it does: validates and grids the environmental data, publishes to `env.clean`.

- **`GET /reference-layers`**
  Query params: `layer_type` (coastline | protected_zone | eez), optional `bbox` to filter spatially.
  What it does: returns the static reference GeoJSON layers loaded at startup — every downstream service that needs "how far is this from the coast" calls this once at its own startup and caches it locally, rather than re-querying constantly.

- **`GET /ingest/status`**
  What it does: returns recent ingestion stats — records accepted/rejected per source type in the last N minutes. Purely a debugging/demo-narration convenience ("here's proof data is flowing").

---

### A2. AIS Analytics Service

- **`GET /vessels/{mmsi}/features`**
  Query params: `window_start`, `window_end` (optional, defaults to latest window).
  What it does: returns the most recent (or a specified historical) behavioral feature row for one vessel — `{avg_speed, speed_variance, course_variance, loitering_score, distance_traveled_km, proximity_events}`. This is what A3, A5's dashboard view, and the Gateway's vessel-detail panel all call.

- **`GET /vessels/{mmsi}/proximity`**
  What it does: returns the current list of other vessels within the proximity threshold of this vessel — a thin, specific slice of the features row, exposed separately because A6 (STS Detection) polls exactly this rather than the full feature object, and it's a natural thing to visualize directly on the dashboard as "who's near whom right now."

- **`POST /features/recompute`**
  Body: `{mmsi (optional — omit to recompute all)}`.
  What it does: manual trigger to force a window recomputation outside the normal streaming cadence — invaluable during Day 2 when you're testing feature logic and don't want to wait for the simulator's real-time pacing.

---

### A3. Anomaly Detection Service

- **`GET /anomalies`**
  Query params: `mmsi` (optional), `since` (optional timestamp), `severity` (optional filter).
  What it does: returns recent anomaly events, filterable — this is the route the dashboard's alert feed queries on load (before the WebSocket takes over for live updates).

- **`GET /anomalies/{event_id}`**
  What it does: returns full detail on one anomaly event including the raw feature values that triggered it (`evidence` field) — used by the incident detail panel so a judge/operator can see *why* something was flagged, not just that it was.

- **`POST /anomalies/evaluate`**
  Body: `{mmsi}` (optional — omit to re-evaluate all vessels against current thresholds).
  What it does: manually re-runs the threshold/z-score check against current feature data without waiting for the next scheduled window — again, a Day 2 debugging convenience, and also useful live in a demo if you want to show "watch what happens when I lower the threshold" for judge engagement.

---

### A4. Dark Vessel Detection Service

- **`GET /dark-vessels`**
  Query params: `since`, `bbox` (optional spatial filter).
  What it does: returns recent dark vessel detections — feeds the dashboard's dark-vessel map layer directly.

- **`GET /dark-vessels/{detection_id}`**
  What it does: full detail on one detection including the source image tile reference and the AIS search radius/window used to confirm no match was found — useful for a judge who asks "how do you know it's actually dark and not just a matching bug."

- **`POST /detections/rerun`**
  Body: `{tile_id}`.
  What it does: manually reprocesses vessel-blob extraction + AIS join for one specific tile — the route you'll hit constantly on Day 1/2 while tuning the blob-detection threshold, since you don't want to replay the entire simulator just to test one image.

---

### A5. AIS Spoof Detection Service

- **`GET /vessels/{mmsi}/trust-score`**
  What it does: returns the current rolling trust score plus the history of individual discrepancy measurements that fed it — `{current_trust_score, flag, history: [{timestamp, discrepancy_distance_m, trust_contribution}]}`. This is a genuinely nice thing to visualize on the dashboard as a small sparkline next to a vessel — "trust score over time" tells a clear story.

- **`GET /spoofing-suspects`**
  Query params: `threshold` (optional override of the default 0.5 cutoff).
  What it does: returns all vessels currently below the spoofing-suspected threshold — this is what A7 (Risk Engine) queries when aggregating, and also a strong standalone dashboard panel.

- **`POST /trust-score/recompute`**
  Body: `{mmsi}`.
  What it does: forces a recompute for one vessel from full history rather than the incremental rolling update — useful if you change the trust-score formula mid-build and need to refresh existing scores rather than wait for new data to trickle in.

---

### A6. STS Detection Service

- **`GET /sts-events`**
  Query params: `since`, `mmsi` (optional, returns events involving this vessel either as vessel_a or vessel_b).
  What it does: returns detected ship-to-ship transfer events — feeds a dashboard overlay (draw a line between the two vessel markers for the event duration).

- **`GET /sts-events/active`**
  What it does: returns pairs currently *in progress* (the state machine hasn't closed the event yet) — distinct from the completed-events route above because "this is happening right now" is a different UI treatment (live alert) than "this happened earlier" (history log).

---

### A7. Vessel Risk Engine

- **`GET /vessels/{mmsi}/risk`**
  What it does: returns `{risk_score, tier, contributing_factors, recommended_action}` — the single most-called route in Module A, since the dashboard colors every vessel marker by this.

- **`GET /vessels/risk?tier=HIGH`**
  Query params: `tier` (optional filter), `min_score` (optional).
  What it does: returns all vessels at or above a risk threshold — this is what drives the "who should we be watching" list panel, and what would (conceptually) trigger satellite tasking in a real deployment.

- **`POST /risk/recompute`**
  Body: `{mmsi}` (optional — omit to recompute all).
  What it does: manual recompute trigger, same rationale as elsewhere — also the natural route to hit right after you've fixed a bug in one of A2–A6 and need risk scores to reflect the correction without a full pipeline replay.

---

## MODULE B — Satellite Spill Intelligence

### B1. SAR Spill Processing

- **`GET /spill-candidates`**
  Query params: `since`, `bbox` (optional), `min_area_km2` (optional).
  What it does: returns raw candidate polygons before look-alike classification — mainly useful for your own debugging (visualizing what B1 alone extracts, before B2 filters it) rather than for the final dashboard, which should show B2's classified output instead.

- **`GET /spill-candidates/{candidate_id}`**
  What it does: full detail including texture_features — this is the exact payload B2 consumes, so exposing it as a route makes B2 easy to test in isolation (call this route, feed the response manually into B2's classifier, confirm sane output) without needing the full streaming pipeline running.

- **`POST /process-tile`**
  Body: `{tile_id}`.
  What it does: manually triggers the full calibrate → filter → threshold → extract pipeline on one specific tile — this is the single most important debug route in the whole system for Module B, since SAR image processing is the part most likely to need visual iteration (run it, look at the output mask, adjust the threshold, run it again) rather than blind pipeline replay.

---

### B2. Look-Alike Engine

- **`GET /classified-spills`**
  Query params: `since`, `final_class` (optional filter: oil | biogenic | low_wind | other).
  What it does: returns classified candidates — this is what C1 (Evidence Fusion) actually consumes, and also a good standalone dashboard debug view ("show me everything we correctly filtered out as NOT oil" is a strong thing to show judges as proof the false-positive reduction works).

- **`GET /classified-spills/{candidate_id}`**
  What it does: full detail including the probability breakdown across all four classes and, if you're using the Random Forest, the feature importances/contributions for that specific prediction — genuinely valuable for the "explainability" story if you have time to surface it.

- **`POST /classify`**
  Body: `{candidate_id}`.
  What it does: manually re-runs classification on one candidate — the route you'll use constantly while iterating on the classifier during Day 2, same rationale as B1's `/process-tile`.

- **`GET /model/info`**
  What it does: returns metadata about the currently loaded classifier — training set size, feature list, last trained timestamp, basic validation metrics if you computed them. Small but useful: lets you (and, if asked, judges) confirm exactly what's running without digging through logs.

---

## MODULE C — Fusion & Attribution

### C1. Multi-Modal Evidence Fusion Service

- **`GET /incidents`**
  Query params: `since`, `min_confidence` (optional).
  What it does: returns fused incident records — the primary route the dashboard's incident list/map layer uses.

- **`GET /incidents/{incident_id}`**
  What it does: returns the full evidence vector breakdown for one incident — `{sar_evidence, ais_evidence, spatial_match, temporal_match, wind_current_match, ais_reliability, overall_confidence}`. This is the route behind the incident detail panel's "why do we believe this" section — render each evidence component as a small bar/score so it's visually obvious which signals contributed most.

- **`POST /fuse`**
  Body: `{candidate_id}` (a B2 classified_spill id to promote and fuse).
  What it does: manually triggers fusion for one candidate — useful when testing the AIS-trust-down-weighting logic specifically: you can call this against a known scenario (a candidate near a vessel you've manually set to low trust) and confirm `overall_confidence` responds correctly, without waiting for a naturally-occurring low-trust vessel to show up in the simulator's playback.

---

### C2. Source Attribution Engine

- **`GET /incidents/{incident_id}/attribution`**
  What it does: returns the full ranked candidate list — `{candidates: [{mmsi, ais_behavior_score, spatial_match, temporal_match, drift_compatibility, vessel_type_score, ais_reliability, final_attribution_probability}], unknown_probability}`. This is the single most visually important route in the whole system — it's the "Vessel A 87% / Vessel B 8%..." breakdown, and should be an easy, obvious call for the dashboard to make and render as a horizontal bar chart.

- **`GET /incidents/{incident_id}/backward-trajectory`**
  What it does: returns the estimated origin polygon/point and the intermediate backward-particle-tracking path — genuinely worth exposing as its own route (rather than folding into the attribution response) because it's a strong standalone map visualization: draw the estimated drift path backward from the spill to the estimated origin, overlaid with the top candidate vessel's actual track, so a viewer can visually see the two lines converge. This single visual is one of your strongest demo moments.

- **`POST /attribute`**
  Body: `{incident_id}`.
  What it does: manually triggers attribution for one incident — same debug rationale as elsewhere, and particularly valuable here since C2 is your most complex service and you'll want to run it repeatedly against the same test incident while tuning the scoring weights.

---

## MODULE D — Forecasting & Severity

### D1. Spill Drift & Forecast Engine

- **`GET /incidents/{incident_id}/forecast`**
  Query params: `horizon` (optional filter to one of 6h/12h/24h/48h/72h; omit for all).
  What it does: returns the predicted polygon(s) — this is what feeds the dashboard's forecast timeline slider directly; the slider just switches which horizon's polygon is displayed on the map.

- **`POST /forecast/generate`**
  Body: `{incident_id}`.
  What it does: manually triggers forecast generation for one incident — used constantly while tuning the advection function (wind-drift factor, time step size) since you'll want to regenerate and re-inspect the resulting polygons repeatedly against the same fixed incident.

---

### D2. Spill Severity & Impact Service

- **`GET /incidents/{incident_id}/severity`**
  What it does: returns the full severity assessment — `{severity, estimated_area_km2, estimated_volume_range_tonnes, growth_rate_pct_per_hr, coastline_distance_km, ecological_exposure}`. Feeds the incident detail panel's severity section directly.

- **`POST /severity/recompute`**
  Body: `{incident_id}`.
  What it does: manual recompute — most useful after D1 regenerates a forecast (since growth_rate depends on it), so you'll typically call this right after hitting D1's `/forecast/generate` during testing.

---

## MODULE E — Decision & Command

### E1. Response Decision Engine

- **`GET /incidents/{incident_id}/recommendations`**
  What it does: returns the ordered `priority_actions` list — feeds the incident detail panel's action checklist directly.

- **`POST /recommendations/generate`**
  Body: `{incident_id}`.
  What it does: manually (re)generates recommendations — you'll want this available live during your demo too, not just for debugging: if a judge asks "what if the severity were lower," you can adjust a value and hit this route live to show the recommendation list change, which is a strong way to demonstrate the system is genuinely rule-driven and explainable rather than hard-coded per demo scenario.

---

### E2. API Gateway

The Gateway's routes are aggregation/proxy routes — each one internally calls the relevant downstream service route(s) above and assembles a single response shaped for the dashboard, so the frontend never needs to know which of the 13 backend services owns which piece of data.

- **`GET /incidents`**
  What it does: proxies C1's `/incidents`, enriched inline with each incident's current severity tier (a quick call to D2) so the dashboard's incident list can show severity without a second round trip.

- **`GET /incidents/{incident_id}/full`**
  What it does: the single route the incident detail panel calls — internally fans out to C1 (`/incidents/{id}`), C2 (`/attribution`, `/backward-trajectory`), D1 (`/forecast`), D2 (`/severity`), and E1 (`/recommendations`), and returns one merged JSON object. This is the most important Gateway route — it's what makes the frontend simple, since it never has to orchestrate six calls itself.

- **`GET /vessels`**
  What it does: proxies A7's `/vessels/risk`, enriched with the latest position from A2, for the map's vessel layer.

- **`GET /vessels/{mmsi}/full`**
  What it does: fans out to A2 (features), A5 (trust score), A7 (risk) for the vessel detail popup on the map.

- **`GET /dark-vessels`**
  What it does: proxies A4 directly (no enrichment needed).

- **`WS /live`**
  What it does: a WebSocket the dashboard connects to once on load. The Gateway itself subscribes to the relevant Redis Streams/Postgres change events (new incident, risk tier change, new dark vessel, new attribution result) and relays each as a small typed event over the socket, e.g. `{type: "new_incident", incident_id}` or `{type: "risk_escalated", mmsi, new_tier}` — the dashboard reacts to each event type by refetching just the relevant piece of data via the REST routes above, rather than the WebSocket carrying full payloads itself (keeps the socket message format simple and the REST routes as the one source of truth for data shape).

---

### E3. Authority Command Dashboard

The dashboard is a frontend, not an API — it has no routes of its own in the backend sense. Worth noting here only because its *pages/views* map directly onto Gateway routes, which is a useful mental checklist while building it:

- Map view ← `GET /incidents`, `GET /vessels`, `GET /dark-vessels`, plus the `/live` WebSocket
- Incident detail panel ← `GET /incidents/{id}/full`
- Vessel detail popup ← `GET /vessels/{mmsi}/full`
- Forecast timeline slider ← the `forecasts` array already included in `/incidents/{id}/full`, just filtered client-side by selected horizon (no extra network call needed per slider move)

---

## Route-Building Priority (what to implement first, per service)

For every service, build routes in this order — it matches how you'll actually use them during the 3-day build:

1. `GET /health` — five minutes, do this the moment the service skeleton exists, before any logic.
2. The **manual trigger POST route** (`/process-tile`, `/classify`, `/attribute`, `/forecast/generate`, etc.) — build this before the streaming worker loop is even finished. It lets you test your core logic function directly via curl/Postman while the Redis plumbing is still being wired, decoupling "does my algorithm work" from "does my event pipeline work" as two separate, separately-debuggable problems.
3. The **primary GET read route** (`/incidents`, `/vessels/{mmsi}/risk`, etc.) — needed as soon as another service or the Gateway wants to consume this service's output.
4. Everything else (filters, detail sub-routes, `/metrics`) — fill in opportunistically on Day 3 if time allows; none of these block integration.
