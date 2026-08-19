# Maritime Oil-Spill Intelligence System — Build Guide (SIH 2026)
### Dockerized Microservices Architecture — 3-Day Execution Plan

---

## 1. System Philosophy for a 3-Day Build

You cannot build real satellite ingestion, real AIS feeds, or a production ML pipeline in 3 days. What you **can** build is a fully working, dockerized, event-driven pipeline that runs on **realistic sample/synthetic data** (public SAR oil-spill datasets, simulated AIS streams) and produces genuinely intelligent outputs end-to-end.

**Golden rule for judges:** a thin, working, end-to-end pipeline with 6 modules talking to each other beats one deep, isolated, unconnected AI model. Build breadth first, depth second, polish third.

Recommended stack (optimized for speed, not "correctness"):
- **Services:** Python + FastAPI for every intelligence service (fast to write, easy to containerize, good ML ecosystem)
- **Message bus:** Redis Streams (lighter than Kafka, trivial to docker-compose, still gives you async/event-driven credit)
- **Database:** PostgreSQL + PostGIS extension (you need geospatial queries — vessel positions, spill polygons, distances)
- **Cache/session:** Redis (same instance, different DB index)
- **Frontend:** React + Leaflet/Mapbox for the Authority Command Dashboard
- **Gateway:** A single API Gateway (FastAPI or Node/Express) that the dashboard talks to — services never talk to the frontend directly
- **Orchestration:** docker-compose (do NOT attempt Kubernetes in 3 days)
- **ML:** scikit-learn / lightweight PyTorch for classifiers; OpenCV + rasterio for SAR image processing; use pretrained/simple models — this is a systems hackathon, not a research paper

---

## 2. Microservices Specification

Each service below maps directly to a box in your architecture diagram. For each: **purpose, input, output, core logic, and priority**.

### MODULE A — Data Pipeline

#### A1. Data Ingestion Service
- **Purpose:** Single entry point for all raw external data; normalizes heterogeneous sources into a common schema before anything else touches them.
- **Input:** Raw AIS stream (JSON/NMEA), SAR satellite tiles (GeoTIFF), optical satellite images (JPEG/PNG + metadata), marine environment data (wind/current/wave — JSON from a weather API or static dataset), geospatial reference layers (coastlines, EEZ boundaries, protected zones — shapefiles/GeoJSON)
- **Output:** Validated, deduplicated, time-synchronized, geo-referenced records published to Redis Streams on topics: `ais.clean`, `sar.clean`, `optical.clean`, `env.clean`
- **Core logic:** schema validation → drop malformed records → deduplicate by (vessel_id/tile_id + timestamp) → interpolate/align timestamps to a common clock → attach lat/lon reference grid
- **Priority:** P0 — everything downstream depends on this

#### A2. AIS Analytics Service
- **Purpose:** Continuous behavioral analysis of every vessel's AIS track (speed, course, heading changes, loitering, course reversals)
- **Input:** `ais.clean` stream
- **Output:** Per-vessel behavioral feature vector written to `vessel_features` table + published to `ais.features` topic (speed_profile, course_variance, loitering_score, proximity_events)
- **Priority:** P0

#### A3. Anomaly Detection Service
- **Purpose:** Flags statistically abnormal vessel behavior (sudden stops, erratic zig-zags, unusual speed for vessel type, off-route deviation)
- **Input:** `ais.features`
- **Output:** `anomaly_events` table — {vessel_id, anomaly_type, severity, timestamp}
- **Core logic:** simple rule-based thresholds + z-score/Isolation Forest on the feature vector — do not over-engineer this in 3 days
- **Priority:** P1

#### A4. Dark Vessel Detection Service
- **Purpose:** Identifies vessels visible in satellite imagery (SAR/optical) but **absent** from AIS at that time/location — i.e., AIS switched off deliberately
- **Input:** `sar.clean`, `optical.clean` (vessel detections from imagery) + `ais.clean` (expected vessel positions)
- **Output:** `dark_vessel_events` — {location, timestamp, image_source, confidence}
- **Core logic:** spatial-temporal join — if imagery detects a vessel-shaped object at (x,y,t) and no AIS broadcast exists within a distance/time tolerance, flag it as dark
- **Priority:** P1 (this is a strong differentiator — keep it, don't cut it)

#### A5. AIS Spoof Detection Service (Innovation 2)
- **Purpose:** Produces an **AIS Trust Score** per vessel by cross-validating AIS-reported position against satellite-observed position
- **Input:** `ais.clean`, satellite vessel detections from A4/SAR pipeline
- **Output:** `ais_trust_scores` — {vessel_id, trust_score (0–1), discrepancy_distance, flag: "spoofing_suspected" if trust < threshold}
- **Core logic:** compute positional discrepancy between claimed AIS position and satellite-observed position at matching timestamps; trust_score = inverse function of discrepancy distance, penalized further by MMSI inconsistency or impossible speed jumps
- **Priority:** P1 — this score becomes a critical **input weight** into the Evidence Fusion service later, so build it early

#### A6. STS (Ship-to-Ship) Detection Service
- **Purpose:** Detects two vessels in prolonged close proximity at low relative speed — classic signature of illegal ship-to-ship transfer, a common precursor to illicit discharge
- **Input:** `ais.clean`
- **Output:** `sts_events` — {vessel_a, vessel_b, duration, location, confidence}
- **Core logic:** pairwise distance under threshold (e.g., <500m) sustained for >X minutes with speed near zero
- **Priority:** P2 (nice-to-have, cut first if time runs short)

#### A7. Vessel Risk Engine
- **Purpose:** Aggregates A2–A6 outputs into one Vessel Risk Score and routes tasking decisions
- **Input:** anomaly_events, dark_vessel_events, ais_trust_scores, sts_events, vessel type/history
- **Output:** `vessel_risk_scores` — {vessel_id, risk_score, tier: LOW/MEDIUM/HIGH}; if MEDIUM/HIGH → publish a `satellite_tasking_request` event; if LOW → just log to monitor
- **Core logic:** weighted sum or simple logistic model over the input flags — keep it explainable (a weighted scorecard is fine and is easier to defend to judges than a black-box model)
- **Priority:** P1

---

### MODULE B — Satellite Spill Intelligence

#### B1. Satellite Spill Intelligence Engine (SAR Processing)
- **Purpose:** Turns raw SAR imagery into candidate oil-slick regions
- **Input:** `sar.clean` GeoTIFF tiles
- **Output:** `spill_candidates` — {polygon geometry, tile_id, timestamp, texture_features}
- **Core logic pipeline:** radiometric calibration → speckle noise filtering (Lee/median filter) → dark-region segmentation (thresholding on backscatter) → candidate polygon extraction
- **Priority:** P0 — this is your core detection capability; use a public SAR oil-spill dataset (e.g., Kaggle "Oil Spill Detection" SAR dataset) so you have real imagery to demo against

#### B2. Look-Alike Engine (Innovation 3)
- **Purpose:** Classifies each dark-SAR candidate as Oil vs Biogenic slick (algae) vs Low-wind area vs Other — this is your false-positive killer
- **Input:** `spill_candidates` + wind data from `env.clean` + vessel proximity from A-module
- **Output:** `classified_spills` — {candidate_id, oil_prob, biogenic_prob, low_wind_prob, other_prob, final_class}
- **Core logic:** feature extraction (shape irregularity, edge sharpness, area, texture entropy) + a simple trained classifier (Random Forest is enough — don't reach for deep learning here given the time budget) using: texture, shape complexity, boundary smoothness, local wind speed (oil persists under low-to-moderate wind; look-alikes often correlate with very low wind or biogenic bloom patterns), temporal persistence across repeat passes, proximity to a vessel track
- **Priority:** P0 — explicitly call this out to judges as your core innovation against false positives

---

### MODULE C — Fusion & Attribution

#### C1. Multi-Modal Evidence Fusion Service
- **Purpose:** Combines AIS evidence, SAR evidence, optical evidence, and environmental data into one confidence-scored incident record
- **Input:** `classified_spills`, `ais_trust_scores`, `vessel_risk_scores`, `env.clean` (wind/current), spatial/temporal proximity of vessels to spill polygon
- **Output:** `fused_incidents` — {incident_id, spill_polygon, evidence_vector: {ais_evidence, sar_evidence, optical_evidence, wind_current_match, spatial_match, temporal_match, ais_reliability}, overall_confidence}
- **Core logic:** each evidence stream contributes a normalized 0–1 score; combine via weighted average or simple Bayesian combination; **AIS reliability score (from A5) should down-weight AIS-derived evidence when spoofing is suspected** — this is the key "explainability" story of your whole system
- **Priority:** P0

#### C2. Source Attribution Engine (Innovation 1)
- **Purpose:** Given a confirmed spill, ranks nearby vessels by probability of causal responsibility
- **Input:** `fused_incidents`, all vessel tracks within a search radius (e.g., 20 km) around spill origin at relevant time window
- **Output:** `attribution_results` — ranked list {vessel_id, ais_behavior_score, spatial_match, temporal_match, drift_compatibility, vessel_type_score, ais_reliability, final_attribution_probability}, normalized to sum to 100% across candidates + "Unknown"
- **Core logic:** **backward particle tracking** — run the drift/forecast model (Module D) in reverse from the observed spill polygon back through time using wind/current fields to estimate the likely origin point and time; compare that estimated origin against each candidate vessel's actual track at that time; combine positional match, temporal match, drift compatibility, vessel type prior (tanker > cargo > fishing, weighted by base rate), and AIS reliability into a per-vessel score; normalize across candidates
- **Priority:** P0 — this is your headline differentiator, prioritize getting a working, even simplified, version over a highly accurate one

---

### MODULE D — Forecasting & Severity

#### D1. Spill Drift & Forecast Engine (Innovation 7)
- **Purpose:** Predicts where the spill will spread over the next 6/12/24/48/72 hours
- **Input:** `fused_incidents` (current spill polygon), `env.clean` (wind/current vector fields)
- **Output:** `drift_forecasts` — {incident_id, horizon: 6h/12h/24h/48h/72h, predicted_polygon, distance_x/y/z, confidence_decay}
- **Core logic:** simplified Lagrangian particle model — seed particles across the spill polygon, advect each particle per timestep using wind (typically ~3% of wind speed vector, standard oil-drift approximation) + current vectors, recompute polygon as the convex hull/alpha-shape of particle positions at each horizon; confidence should decay with forecast horizon
- **Priority:** P0 — also feeds C2 (backward tracking is the same model run in reverse)

#### D2. Spill Severity & Impact Service (Innovation 6)
- **Purpose:** Converts raw detection into a severity classification authorities can act on
- **Input:** `fused_incidents`, `drift_forecasts`, coastline/protected-zone layers from ingestion
- **Output:** `severity_assessments` — {incident_id, severity: LOW/MEDIUM/HIGH/CRITICAL, estimated_area_km2, estimated_volume_range_tonnes (with explicit uncertainty, never a single number), growth_rate_pct_per_hr, coastline_distance_km, ecological_exposure: LOW/MEDIUM/HIGH}
- **Core logic:** area from polygon geometry; volume as a **range** using published oil-thickness-class heuristics (thin sheen vs thick slick, itself inferred from SAR backscatter intensity) — explicitly state this is an estimate with uncertainty, not a precise measurement (be upfront about this to judges, it shows technical maturity rather than overclaiming); ecological exposure from proximity to mangrove/fishing-zone/protected-area layers
- **Priority:** P1

---

### MODULE E — Decision & Command

#### E1. Response Decision Engine (Innovation 5)
- **Purpose:** Converts detection + attribution + severity + forecast into a ranked, actionable checklist for authorities
- **Input:** `fused_incidents`, `attribution_results`, `severity_assessments`, `drift_forecasts`
- **Output:** `response_recommendations` — {incident_id, priority_actions: ordered list e.g. "Deploy containment", "Notify coastal authority", "Monitor fishing zone", "Track probable source vessel"}
- **Core logic:** rule-based decision tree keyed on severity tier + ecological exposure + coastline ETA — keep this explainable and rule-based, not ML; judges will want to see *why* each recommendation fires
- **Priority:** P1

#### E2. Authority Command Dashboard (Frontend + BFF)
- **Purpose:** The single pane of glass — live maritime map, incident feed, and command actions
- **Input:** all of the above via the API Gateway (REST + a WebSocket/SSE channel for live updates)
- **Output:** rendered UI — vessel layer, spill layer, dark-vessel layer, risk-zone layer, forecast overlay (timeline slider for +6h/+12h/.../+72h), incident detail panel (incident ID, risk level, source vessel + confidence, spill area, predicted spread, recommended actions)
- **Priority:** P0 — this is what the judges actually watch; do not shortchange this even though it's "just UI"

#### E3. API Gateway
- **Purpose:** Single, stable entry point for the dashboard; hides internal service topology; handles auth (even a simple API key for the demo)
- **Input:** dashboard requests
- **Output:** aggregated responses from downstream services; also relays real-time events (spill detected, risk elevated) to the dashboard over WebSocket/SSE
- **Priority:** P0

---

### MODULE F — Infrastructure (cross-cutting)

- **PostgreSQL + PostGIS container:** single source of truth for all structured/geospatial data
- **Redis container:** message bus (Streams) + cache
- **docker-compose.yml:** orchestrates all of the above, one network, named volumes for Postgres data
- **Seed/synthetic data generator:** a standalone script (not a "service") that replays a public SAR oil-spill dataset and a simulated AIS stream at accelerated speed into the ingestion service — this is what makes your demo look "live"

---

## 3. Project Structure

```
maritime-oilspill-system/
├── docker-compose.yml
├── .env.example
├── README.md
├── infra/
│   ├── postgres/
│   │   └── init.sql                  # PostGIS extension + schema DDL
│   └── redis/
│       └── redis.conf
├── data/
│   ├── sample_sar/                   # public SAR oil-spill dataset tiles
│   ├── sample_ais/                   # simulated/sample AIS tracks (csv/json)
│   ├── env_layers/                   # wind/current sample data
│   └── geo_layers/                   # coastline, protected zones, EEZ (GeoJSON)
├── services/
│   ├── data-ingestion/
│   │   ├── Dockerfile
│   │   └── app/
│   ├── ais-analytics/
│   │   ├── Dockerfile
│   │   └── app/
│   ├── anomaly-detection/
│   ├── dark-vessel-detection/
│   ├── ais-spoof-detection/
│   ├── sts-detection/
│   ├── vessel-risk-engine/
│   ├── sar-spill-intelligence/
│   ├── lookalike-engine/
│   ├── evidence-fusion/
│   ├── source-attribution/
│   ├── drift-forecast/
│   ├── severity-impact/
│   ├── response-decision/
│   └── api-gateway/
│       ├── Dockerfile
│       └── app/
├── dashboard/
│   ├── Dockerfile
│   └── src/
├── simulator/
│   ├── Dockerfile
│   └── replay_ais_and_sar.py-equivalent   # data replay/seeding logic (described, not coded here)
└── docs/
    ├── architecture.md
    └── api-contracts.md
```

Each service folder is a self-contained container with its own Dockerfile, own dependency file, and a single `app/` entrypoint exposing a small REST API + a background worker that subscribes to its input Redis stream and publishes to its output stream/table. This uniform shape is what lets you build 13 services in 3 days — every service is a copy-paste of the same skeleton with different logic inside.

---

## 4. Module-to-Team-Member Mapping

If your team has 4–5 people, split by module, not by "frontend/backend":

| Module | Services | Suggested owner |
|---|---|---|
| A — Data Pipeline & Vessel Intelligence | Ingestion, AIS Analytics, Anomaly, Dark Vessel, Spoof, STS, Risk Engine | Person 1 (+Person 2 for A4/A5, the differentiators) |
| B — Satellite Spill Intelligence | SAR Processing, Look-Alike Engine | Person 2/3 (whoever is strongest at CV/image processing) |
| C — Fusion & Attribution | Evidence Fusion, Source Attribution | Person 3/4 (needs to integrate outputs of A and B, so this person should be free from Day 1 tasks) |
| D — Forecasting & Severity | Drift Forecast, Severity/Impact | Person 4 |
| E — Decision & Command | Response Decision, Dashboard, Gateway | Person 5 (or whoever is strongest frontend) |
| F — Infra | docker-compose, DB schema, seed data | Shared — set up on Day 1 morning before splitting |

---

## 5. Day-by-Day Execution Plan

### DAY 1 — Foundation & Data Spine (target: by end of day, raw data is flowing through Postgres/Redis and every service skeleton exists and runs in Docker)

**Morning (0–4h):**
1. Set up the repo structure above; write `docker-compose.yml` with Postgres+PostGIS and Redis containers running and reachable.
2. Design and finalize the Postgres schema (vessels, vessel_positions, spill_incidents, attribution_results, forecasts, severity, response_recommendations) — do this once, as a team, so no service builds against a moving schema.
3. Acquire data: download a public SAR oil-spill imagery dataset (Kaggle/ESA sample), build or download a small realistic AIS track dataset (a few dozen vessels, a few days of positions), grab a coastline/protected-area GeoJSON for your target region, get a simple wind/current sample dataset.
4. Scaffold every service folder with the identical skeleton (Dockerfile + minimal FastAPI app that exposes `/health` and can publish/subscribe to Redis) — get all 13+ containers building and running in compose even though they do nothing yet.

**Afternoon (4–8h):**
5. Build the **Data Ingestion Service** for real — this unblocks everyone else.
6. Build the **simulator/replay script** that reads the sample AIS and SAR data and streams it into ingestion at demo-friendly speed (this is what makes your live demo compelling).
7. Each module owner starts building their P0 service's input/output contract against the schema (even if internal logic is stubbed/returns a placeholder value) — the goal by end of Day 1 is that data flows: raw data → ingestion → clean streams → at least one stub consumer per module writing rows to Postgres.
8. Stand up the API Gateway skeleton and a bare dashboard shell that can hit `/health` on the gateway and render a blank map.

**End of Day 1 checkpoint:** docker-compose up brings up the whole stack; simulated data is visibly flowing into the database; every service container is healthy.

### DAY 2 — Intelligence (target: by end of day, every P0 service does its real job in isolation)

**Morning (0–4h):**
- Module A: implement AIS Analytics feature extraction, Anomaly Detection thresholds, Dark Vessel spatial-temporal join, AIS Spoof trust scoring.
- Module B: implement SAR calibration/speckle filtering/candidate extraction; get the Look-Alike Engine classifying candidates using at least texture + wind features (train on whatever labeled samples your dataset provides, or hand-label a small set yourself if the dataset lacks labels — a working weak classifier beats no classifier).

**Afternoon (4–8h):**
- Module A: finish Vessel Risk Engine (aggregates A2–A6 into risk tiers).
- Module C: begin Evidence Fusion — this needs A's and B's outputs, so start integration testing here, don't wait until Day 3.
- Module D: implement the particle-drift forecast model (forward direction first — 6h/12h/24h/48h/72h polygons from a spill).
- Module E: dashboard team starts wiring the live map — vessel markers, spill polygons — against whatever is in Postgres so far, even partial data.

**End of Day 2 checkpoint:** you can manually insert/observe a spill incident and see risk scores, a classified spill (oil vs look-alike), and a forecast polygon — even if source attribution isn't wired yet.

### DAY 3 — Integration, Attribution, Decision Layer, Demo Polish

**Morning (0–4h):**
1. Module C: finish **Source Attribution Engine** — reuse Module D's drift model in reverse (backward particle tracking) to estimate spill origin, then score nearby vessels. This is your headline feature — protect time for it.
2. Module D: finish Severity & Impact Service.
3. Module E: finish Response Decision Engine (rule-based tree over severity + attribution + forecast).
4. Full pipeline integration test: run the simulator end-to-end and confirm an incident flows all the way from raw SAR/AIS → classified spill → fused evidence → attributed vessel → forecast → severity → recommended actions → visible on the dashboard.

**Afternoon (4–8h):**
5. Dashboard polish: incident detail panel, forecast timeline slider, risk-zone overlay, dark-vessel markers, source-attribution breakdown (the "Vessel A 87% / Vessel B 8% / ..." view is a great visual moment — prioritize it).
6. Fix integration bugs found during the morning's end-to-end run — budget real time for this, it always takes longer than expected.
7. Write `docs/architecture.md` and `docs/api-contracts.md` briefly — judges and evaluators often check for documentation.
8. Prepare a scripted demo scenario: one clean "everything works" incident walkthrough (dark vessel appears → spill detected → classified as oil not look-alike → AIS spoofing flagged on the true source vessel, lowering its evidence weight but attribution engine still ranks it top via spatial/drift match → forecast shown → severity CRITICAL → recommended actions fire). This exact narrative demonstrates every one of your 6 innovations in under 3 minutes.
9. Rehearse the demo twice. Have a fallback: pre-recorded screen capture in case live Docker demo has issues on unfamiliar wifi/hardware.

**End of Day 3 checkpoint:** full docker-compose stack runs from a single command on a clean machine; scripted demo scenario plays cleanly start to finish; README explains how to run it.

---

## 6. Cut List (if you fall behind schedule)

Cut in this order — each is decoupled enough that removing it doesn't break the pipeline:
1. STS Detection (A6) — least central to the "who caused it" story
2. Anomaly Detection (A3) — Vessel Risk Engine can run on Dark Vessel + Spoof scores alone
3. Reduce Severity/Impact (D2) to area + growth rate only, drop volume estimation entirely
4. Reduce forecast horizons to just 6h/24h/72h instead of all five
5. Never cut: Ingestion, SAR + Look-Alike Engine, Evidence Fusion, Source Attribution, Dashboard — these are the demo's spine and its differentiators.
