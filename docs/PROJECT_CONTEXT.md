# Aqua Sentinel — Project Context (for LLM consumption)

> This document is a single, self-contained and *verbatim-to-code* explanation of the
> **Aqua Sentinel** project: what it is, what is implemented, how the pieces connect,
> what is real vs. demo, and what is unfinished. It exists so another LLM can be dropped
> into this repository and immediately understand it without re-reading every file.
> Everything below was cross-checked against the current code in the repo
> (`docker-compose.yml`, `services/*/app/*.py`, `infra/postgres/init.sql`,
> `frontend/`, `docs/`).

---

## 1. What the project is

**Aqua Sentinel** is a maritime intelligence system (built for the Smart India
Hackathon) that:

1. **Detects** oil spills from **SAR satellite imagery** (Sentinel-1) and from
   **AIS vessel behavior**,
2. **Fuses** the two independent evidence streams,
3. **Attributes** a spill to a likely source vessel,
4. **Forecasts** drift, **assesses** severity/impact, and
5. **Recommends** priority response actions.

The target operating region is the **Mumbai offshore AOI** (bounding box approx.
`14–25°N, 68–77.5°E`).

### Architecture in one line
A pipeline of **Python 3.11 + FastAPI microservices** that exchange events over
**Redis Streams**, persist spatio-temporal data in **PostgreSQL 15 + PostGIS**, and
serve live **web dashboards** through an **API gateway**.

### Key infrastructure facts
- **Redis 7** — stream/message bus (`redis:7-alpine`).
- **PostgreSQL 15 + PostGIS** — central store (`postgis/postgis:15-3.4`).
- **17 microservices** (one per table row in README; some share hosts).
- **Two frontends exist**: `frontend/` (active, MapLibre/TypeScript — this is what
  `docker-compose` builds for the `dashboard` service on port 3000) and `dashboard/`
  (older React-Leaflet/JS prototype, kept but **not** the deployed one).
- `docker-compose up --build` brings the whole stack up; bootstrap from README.

> **Important nuance:** some older docs (`docs/how-to-run.md`, written before recent
> commits) still describe `source-attribution`, `drift-forecast`, `severity-impact`,
> and `response-decision` as "heartbeat-only skeletons". **This is no longer true in
> the current code** — all four now have fully wired workers consuming real streams
> (confirmed by reading their `app/worker.py`). Trust the code, not the stale doc.

---

## 2. The two main data pipelines

The system has two independently exercisable flows that share the same DB + Redis:

### Pipeline A — AIS / vessel-risk (fully wired, works live)
```
AISStream.io (websocket) or POST /ingest/ais
   │
   ▼
data-ingestion          validate → dedup → normalise → upsert vessel → insert
   │                    position → publish ais.clean      (+ weather poll loop)
   ▼
ais-analytics           buffers 15-min windows → behavioral feature vector
   │                    → vessel_features table → publish ais.features
   ▼
anomaly-detection       rules + Welford/z-score + Isolation Forest (ML)
   │                    → anomaly_events → publish anomaly.events
   ▼
ais-spoof-detection     (from ais.clean) 5-factor trust scoring → ais_trust_scores → ais.trust
sts-detection           (from ais.features) state machine → sts_events → sts.events
   ▼
vessel-risk-engine      (consumes anomaly.events + ais.trust + sts.events)
   │                    → vessel_risk_scores + satellite_tasking_requests → vessel.risk
   ▼
api-gateway ── REST + WebSocket ──► frontend dashboard

### Pipeline B — SAR oil-spill (implemented; end-to-end only via synthetic demo)
```
data-ingestion          SAR acquisition via Google Earth Engine (Run A / real)
   │                    OR synthetic slick injection (Run B / demo)
   │                    → publish sar.clean (GeoTIFF path + scene_metadata)
   ▼
sar-spill-intelligence  Lee despeckle → Otsu dark segmentation ∪ CFAR →
   │                    morphology → polygonize → spill_candidates (PostGIS)
   │                    + scene artifact bundle (npy on docker volume)
   │                    → publish spill.candidates.raw (per scene)
   ▼
lookalike-engine        Step 4 shape heuristics (ship_shadow / calm_water / slick)
   │                    Step 5 GLCM texture + heuristic confidence → spill_candidates
   │                    → publish spill.candidates.filtered (possible_oil_spill only)
   ▼
evidence-fusion         fuse candidate with high-risk vessels (PostGIS ST_DWithin
   │                    + temporal window) → publish incident.fused
   ▼
source-attribution      score source vessels → create spill_incidents +
   │                    attribution_results → publish spill.attributed
   ├─────────────┬──────────────────────────┤
   ▼             ▼                          ▼
drift-forecast  severity-impact            (feeds each other / next)
forecast tiles  severity table              → spill.severity
(spill.forecast)                            → response-decision
                                             → spill.response (recommendations)
```

### Auxiliary flows
- **dark-vessel-detection** — two mechanisms: (1) periodic AIS-gap scanner of the
  `vessels` table, and (2) SAR-correlation consumer on `sar.clean` that finds SAR
  objects with no matching AIS ping → `dark_vessel_events` → `dark.vessel.events`.

---

## 3. Full service inventory (actual implementation state)

| # | Service (dir) | Port | Consumes → Produces | Implemented logic |
|---|---|---|---|---|
| 1 | `data-ingestion` | 8001 | live AIS + `POST /ingest/ais` → `ais.clean`; GEE → `sar.clean` | validation, Redis dedup, normalisation, vessel upsert, position insert, weather polling (Open-Meteo), SAR acquisition + synthetic injection |
| 2 | `ais-reader` | — | (helper) AIS provider reader | optional provider reader (`aisstream` / `vesselapi`); see `run_ais_reader.py` |
| 3 | `ais-analytics` | 8002 | `ais.clean` → `ais.features` | 15-min window buffer, behavioral feature extraction, cross-vessel proximity, port near check |
| 4 | `anomaly-detection` | 8003 | `ais.features` → `anomaly.events` | 8 heuristic rules, Welford online stats + z-score, Isolation Forest ML (auto-retrain on volume), weather-aware suppression |
| 5 | `dark-vessel-detection` | 8004 | `sar.clean` → `dark.vessel.events` | AIS-gap scan + SAR–AIS spatial/temporal correlation |
| 6 | `ais-spoof-detection` | 8005 | `ais.clean` → `ais.trust` | 5-factor trust scoring + rolling EMA |
| 7 | `sts-detection` | 8006 | `ais.features` → `sts.events` | ship-to-ship state machine, stale-pair close |
| 8 | `vessel-risk-engine` | 8007 | anomaly/trust/sts → `vessel.risk` | weighted risk score, tiering, decay, satellite tasking request |
| 9 | `sar-spill-intelligence` | 8008 | `sar.clean` → `spill.candidates.raw` | Lee despeckle, Otsu segmentation, CFAR, morphology, polygonize, artifact bundle |
| 10 | `lookalike-engine` | 8009 | `spill.candidates.raw` → `spill.candidates.filtered` | shape heuristics, GLCM texture, heuristic confidence scoring |
| 11 | `evidence-fusion` | 8010 | `vessel.risk` + `spill.candidates.filtered` → `incident.fused` | PostGIS geography + temporal correlation, nearest-vessel selection |
| 12 | `source-attribution` | 8011 | `incident.fused` → `spill.attributed` | multi-factor attribution scoring (distance/time/trajectory/wind/behavior), creates `spill_incidents` + `attribution_results` |
| 13 | `drift-forecast` | 8012 | `spill.attributed` → `spill.forecast` | Lagrangian drift model → forecast polygons |
| 14 | `severity-impact` | 8013 | `spill.attributed` → `spill.severity` | area saturation, protected-area & coastline proximity, severity table |
| 15 | `response-decision` | 8014 | `spill.severity` → `spill.response` | priority recommendation rules → `response_recommendations` |
| 16 | `api-gateway` | 8015 | — | unified REST API + WebSocket `/live` relay + `/system/*` monitoring |
| 17 | `dashboard` | 3000 | — | active React/TypeScript/MapLibre UI (built from `frontend/`) |

Host ports: DB `5433`, Redis `6380` (host) — configured to avoid clashing with local
Postgres/Redis.

---

## 4. Shared infrastructure / conventions

### Redis Streams (the message bus)
Services consume via a shared `consume_stream`/`ensure_consumer_group` helper with
consumer groups, and publish via `publish_to_stream`.

Core stream names: `ais.clean`, `ais.features`, `anomaly.events`, `ais.trust`,
`sts.events`, `vessel.risk`, `sar.clean`, `spill.candidates.raw`,
`spill.candidates.filtered`, `incident.fused`, `spill.attributed`,
`spill.forecast`, `spill.severity`, `spill.response`, `dark.vessel.events`.

### PostgreSQL / PostGIS
- Schema in `infra/postgres/init.sql` (auto-initialised on a fresh volume).
- ~20+ tables incl.: `vessels`, `vessel_positions`, `vessel_features`,
  `anomaly_events`, `ais_trust_scores`, `sts_events`, `vessel_risk_scores`,
  `dark_vessel_events`, `satellite_tasking_requests`, `spill_candidates`,
  `spill_incidents`, `attribution_results`, `forecasts`, `severity`,
  `response_recommendations`, `environmental_conditions`, `reference_layers`,
  `vessel_running_stats`, `ingest_stats`, `protected_areas`.
- **Spatial rule (critical):** coordinates stored as `GEOMETRY(...,4326)` (degrees).
  For *metre* distances always cast to `::geography`
  (`ST_Distance(geom::geography, geom::geography)` = metres;
  `ST_DWithin(geom::geography, geom::geography, radius_m)`).
  Topology (`ST_Intersects/Contains/Within`) stays geometry-based.
  Point order is always `POINT(longitude latitude)`.
- Shared helpers in `shared/spatial/` and `services/shared/geo_utils.py`.

### SAR artifact bundle (no rasters in Postgres)
Raster pixels are stored **never** in Postgres. Each processed scene writes one
bundle to the Docker named volume `sar-scene-artifacts` (mounted at
`SAR_ARTIFACT_ROOT`, default `/data/artifacts`), keyed by `scene_id`:
`raw_image.npy` (pre-despeckle), `filtered_image.npy` (Lee-filtered), dark-mask,
SAR-intensity region, bright-target mask, `metadata.json`.

### 5.1 AIS ingestion & cleaning (`data-ingestion`)
- Live websocket to AISStream.io (or `POST /ingest/ais` batch as substitute).
- Per message: `validate_ais_record` → Redis **dedup** → `normalize_timestamp` →
  `upsert_vessel` → `insert_position` → publish `ais.clean`.
- Tracks `ingest_stats` (accepted/rejected + reasons) exposed at `/ingest/status`.
- Runs a weather polling loop (Open-Meteo Marine) writing `environmental_conditions`
  (wind speed/direction, current speed/direction) — used to suppress false anomalies.

### 5.2 Behavioral analytics (`ais-analytics`)
- Buffers pings per vessel; flushes a feature window every `WINDOW_MINUTES` (15).
- `compute_features` produces: avg/max speed, speed variance, course variance,
  heading-change rate, loitering score, distance travelled, proximity_events
  (nearby vessels), plus finer behavioral signals (COG–heading divergence, max
  rate-of-turn, turn reversals, draught/draught-change).
- Writes `vessel_features`, publishes `ais.features`.

### 5.3 Anomaly detection (`anomaly-detection`) — 3 detection layers
- **Rules (8):** `sudden_stop`, `erratic_course`, `speed_anomaly`,
  `loitering_anomaly`, `route_deviation`, `ais_gap`, `draught_drop` (plus
  COG/heading + turn-reversal checks). Weather-aware (suppresses erratic-course /
  sudden-stop flags in storms).
- **Statistical:** Welford's online algorithm per vessel (mean/M2 in Redis), then
  z-score flagging, falling back to population priors when a vessel has < 5 windows.
- **ML:** `IsolationForest` (scikit-learn), auto-retrained hourly on the last 48 h of
  `vessel_features` (needs ≥ 100 samples), persisted to a named volume
  (`ml-models`), warm-started on boot, thread-safe model swap, severity calibrated
  from the score distribution. Emits `ml_behavioral_anomaly` events.

### 5.4 AIS spoof detection (`ais-spoof-detection`) — 5-factor trust
Factors (weighted): **1)** dead-reckoning position check (35%) — projects expected
position from last speed/heading/time and measures discrepancy;
**2)** speed jump / teleport (25%); **3)** MMSI collision — same MMSI reporting from
two impossibly far locations (20%); **4)** identity consistency — name/type changed for
same MMSI (15%); **5)** MMSI validity / ITU MID prefix (5%). Combined into an instant
score, smoothed by a **rolling EMA** (starts trusted at 1.0). Below
`SPOOFING_TRUST_THRESHOLD` (0.5) → `spoofing_suspected` flag; writes `ais_trust_scores`
and publishes `ais.trust`.

### 5.5 STS (ship-to-ship transfer) detection (`sts-detection`)
State machine over per-pair (MMSI A ↔ B) proximity events from `ais.features`:
tracks co-location within `STS_DISTANCE_THRESHOLD_M` (500 m) and low combined speed,
promotes to an `sts_events` row when sustained; closes stale pairs after a timeout.

### 5.6 Vessel risk engine (`vessel-risk-engine`)
Recomputes a per-vessel risk score (0–100) from current DB state: recent anomalies
(168 h), latest trust score, STS history, dark-vessel proximity, time-since-last-seen
(decay), vessel type, and nearest weather. Produces tier (`LOW/MEDIUM/HIGH/CRITICAL`),
`contributing_factors` breakdown, and a `recommended_action`. Upserts
`vessel_risk_scores`, publishes `vessel.risk`. For **HIGH/CRITICAL** vessels it writes
a `satellite_tasking_requests` row (automatic satellite re-tasking).


### 5.7 SAR spill intelligence (`sar-spill-intelligence`) — Steps 1–3
- **Step 1 Lee despeckle** (`despeckle.py`): vectorised local-statistics speckle filter.
- **Step 2 detection**: **Otsu** dark-region segmentation (on the lower-median
  backscatter subset so the dark class stays the minority) **∪** vectorised **CFAR**
  annulus (guard-cell constant-false-alarm-rate) for small dark targets
  (ship shadows).
- **Step 3** morphology (open 3 / close 5, min area via `SAR_MIN_AREA_M2`) →
  **polygonize** connected components → GeoJSON polygons → `spill_candidates`
  (PostGIS, `status='raw'`) + scene artifact bundle → publish `spill.candidates.raw`.

### 5.8 Lookalike engine (`lookalike-engine`) — Steps 4–5
- **Step 4** shape heuristics classify each candidate: `likely_ship_shadow`,
  `likely_calm_water`, or `possible_slick`.
- **Step 5** for `possible_slick`, computes **GLCM texture** features on the *raw*
  (pre-filtered) crop, then a weighted heuristic confidence = f(darkness 30%, texture
  20%, shape 20%, area 10%, context 20%) with a ship-shadow penalty. Labels:
  `possible_oil_spill` (conf > 0.5) else `low_confidence`. Publish to
  `spill.candidates.filtered` only for `possible_oil_spill`.
- **Provenance:** `is_synthetic` flag (for demo-injected data) is carried unchanged
  through every boundary. No `confirmed`/ground-truth label exists anywhere.


### 5.9 Evidence fusion (`evidence-fusion`)
For each filtered SAR candidate: resolve centroid + acquisition time in PostGIS;
query **high-risk vessels** (tier HIGH/CRITICAL) within spatial window
(`EVIDENCE_SPATIAL_WINDOW_M`, 5 km) and temporal window
(`EVIDENCE_TEMPORAL_WINDOW_HOURS`, ±6 h) using PostGIS geography `ST_DWithin`;
select deterministically the **nearest** correlated vessel; publish `incident.fused`.
**Fusion only** — `correlated_vessel_id` is context, never source attribution.

### 5.10 Downstream pipeline (source-attribution → drift → severity → response)
- **source-attribution** (`attribution.py`): scores nearby vessels with weighted
  sub-scores — distance, temporal alignment, trajectory agreement, wind-drift
  agreement, behavior — into a `final_score`; creates the `spill_incidents` row and
  `attribution_results`; publishes `spill.attributed`.
- **drift-forecast** (`drift.py`): Lagrangian particle drift model using
  wind/current → forecast polygons (multiple horizons) into `forecasts` →
  `spill.forecast`.
- **severity-impact** (`severity.py`): area saturation + proximity to protected areas
  and coastline (PostGIS `ST_DWithin`) → severity level/score + exposure flags →
  `severity` table → `spill.severity`.
- **response-decision** (`rules.py`): priority recommendation engine →
  `response_recommendations` → `spill.response` (priority enum LOW/MEDIUM/HIGH/URGENT).

### 5.11 Dark-vessel detection (`dark-vessel-detection`)
- **(1) AIS-gap scan** (periodic, every `DARK_VESSEL_SCAN_INTERVAL_S`): finds recently
  active vessels now silent beyond `DARK_VESSEL_GAP_MINUTES` (90) and **not in port**
  (spatial check vs `reference_layers`); scores by gap duration, vessel type, EEZ
  position, recent STS.
- **(2) SAR correlation**: consumes `sar.clean`; performs a spatio-temporal join
  against `vessel_positions` (within `DARK_VESSEL_MAX_MATCH_DISTANCE_M` and
  `DARK_VESSEL_MAX_MATCH_TIME_S`); no AIS match → emit a `dark_vessel_events` row.


---

## 6. API gateway & live frontend

### API gateway (`AIS/services/api-gateway`)
Unified REST + WebSocket:
- **System:** `GET /health`, `GET /system/health` (aggregate), `GET /system/pipeline`
  (stream lengths + consumer lag), `GET /system/stats`.
- **Vessels:** `/vessels`, `/vessels/{mmsi}`, `/vessels/{mmsi}/track`,
  `/vessels/{mmsi}/risk`, `/vessels/{mmsi}/anomalies`, `/vessels/{mmsi}/trust`,
  `/vessels/risk/leaderboard`.
- **Events:** `/anomalies`, `/sts`, `/sts/active`, `/spoofing/suspects`,
  `/risk/vessels`, `/risk/tasking-requests`, `/features`.
- **Spill/intelligence:** `/spill/candidates/{id}`, `/spill/incidents`,
  `/spill/incidents/{spill_id}` (returns incident + severity + top-5 attribution +
  forecasts + recommendations), dark vessels, drift forecast.
- **Live:** WebSocket `/live` pushes `anomaly`, `sts`, `risk`, `spill_candidate`,
  `incident_fused`, plus a 10 s heartbeat.

### Active frontend (`frontend/`) — React + TypeScript + MapLibre GL
Features in `App.tsx` / `components/*`:
- Interactive **MapLibre** map centered on the Mumbai AOI.
- Layer toggles: **vessels, dark vessels, protected areas**.
- Live **feed** (spill/risk/dark/system events) from WS + historical feed.
- **Flagged vessels** sidebar (critical/high) with risk score + SAR-tasking status.
- **Incidents** list + **IncidentDetailsPage** (severity, attribution, exposure,
  confidence, drift forecast with horizon selector).
- **VesselDetailsPage** (profile, risk factors).
- **SARTaskingPipeline** component — visual SAR tasking/acquisition flow.
- `dashboard/` is an older React-Leaflet prototype — **not** the deployed UI; the
  deployed service builds `frontend/` (`docker-compose.yml` → `dashboard: build: ./frontend`).


---

## 7. Data / demo seeding & scripts

- `infra/postgres/init.sql` — full schema + enums + GIST/btree indexes (~20 tables).
- `scripts/seed_demo_data.py` — seeds vessels, positions, one spill, attribution,
  forecasts, severity, recommendations, protected areas, environmental observations.
- `scripts/verify_db.py` — schema verification.
- `sar/scripts/generate_synthetic_sar_fixture.py` — deterministic synthetic SAR fixture.
- `sar/scripts/demo_sar_spill.py` — end-to-end SAR demo: `--run-a` (real Sentinel-1 via
  GEE) and `--run-b` (synthetic injection validating `is_synthetic` provenance),
  or both by default.
- `run_ais_reader.py` — standalone AIS reader entrypoint.

## 8. Tests

- `tests/` — **spatial regression tests** (meter-vs-degree distance, `ST_DWithin`
  radius semantics, geometry topology, lon/lat order) — run against a container.
- Per-service suites (in-process; DB-backed tests skip when PostGIS unreachable):
  `sar/sar-spill-intelligence/test_*` (28), `lookalike-engine/test_*` (37),
  `evidence-fusion/test_*` (19), `sar/tests/test_synthetic_injection.py` (10),
  plus `tests/test_spatial.py`, `sar/tests/test_sar_acquisition.py`. Provenance tests
  stub shared infra so workers are tested without live Redis/PostGIS.

## 9. Docker / infra layout

- `docker-compose.yml` defines infra (postgres, redis), backend services (via a
  `x-backend-defaults` YAML anchor), `api-gateway`, `dashboard` (builds
  `frontend/`), `simulator` (mounts `./data` read-only; prints + sleeps).
- Named volumes: `pgdata`, `sar-scene-artifacts`, `ml-models`.
- Secrets: `.env` (git-ignored) holds `POSTGRES_*`, `AISSTREAM_API_KEY`,
  `VESSELAPI_API_KEY`, `GEE_SERVICE_ACCOUNT`; GEE private key JSON mounted read-only
  from `./secrets/gee-key.json` → `/run/secrets/gee-key.json`.
- `Makefile` — orchestration targets (`up`, `down`, `logs`, `clean`, etc.).

## 10. Known limitations

1. **Real SAR acquisition needs Google Earth Engine creds + Drive export perms** —
   without them the SAR trigger logs an auth error. The *verified runnable* SAR path
   is the **synthetic demo (Run B)**.
2. **Live AIS needs `AISSTREAM_API_KEY`/`VESSELAPI_API_KEY`**; without a key
   `ais-reader` idles — `POST /ingest/ais` is the substitute.
3. SAR thresholds (`SAR_MIN_AREA_M2`, `SAR_BRIGHT_TARGET_THRESHOLD`) and
   evidence-fusion windows (`EVIDENCE_*`) are fixture/tunable values, **not validated**.
4. `how-to-run.md` still calls source-attribution / drift-forecast /
   severity-impact / response-decision skeletons — **stale**; current code has them
   fully wired.
5. `simulator/` does **not** push data into the pipeline (prints + sleeps).
6. Node frontends need `npm install`/build; `dashboard/` is a prototype — deployed UI
   is `frontend/`.

## 11. Quick start

```bash
cp .env.example .env          # fill in real keys as needed
docker-compose up --build     # full stack
# UI:           http://localhost:3000
# API health:   http://localhost:8015/health
# DB:           localhost:5433 (aqua_sentinel)
# Redis:        localhost:6380
```
For the SAR demo: `docker compose exec data-ingestion python /app/demo_sar_spill.py`
(needs GEE creds); synthetic validation path is `--run-b`.

```