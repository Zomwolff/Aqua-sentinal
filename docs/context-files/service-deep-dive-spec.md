# Service-by-Service Deep Dive
### What each service does, exact I/O, how the logic works, and how to build it

This document goes one level deeper than the build guide. For every service: the story of what's happening inside it, the exact data it consumes and produces, the step-by-step logic, and a concrete build path.

Convention used throughout: every service is a small FastAPI app with two halves —
- a **worker** (background loop) that subscribes to a Redis Stream, processes each message, writes results to Postgres, and publishes to its output stream
- a **thin API** (`/health`, and a couple of read endpoints so the Gateway/Dashboard can query current state directly instead of only via streams)

This shape never changes across services — only the "process one message" logic inside differs. Internalize this once and every service below becomes a fill-in-the-blank exercise.

---

## MODULE A — Data Pipeline & Vessel Intelligence

### A1. Data Ingestion Service

**What's happening:** This is the airlock between the messy outside world and your clean internal system. Nothing downstream should ever have to think about malformed timestamps, duplicate AIS pings, or misaligned coordinate systems — that all gets resolved here, once.

**Input:**
- AIS stream records: `{mmsi, lat, lon, speed_knots, course, heading, timestamp, vessel_type, vessel_name}` (JSON, arriving on a Redis Stream `raw.ais` — in your case, pushed by the simulator)
- SAR tiles: GeoTIFF files (or file paths + metadata JSON: `{tile_id, sensor, capture_timestamp, bbox, file_path, polarization}`)
- Optical tiles: same shape as SAR but `sensor: optical`
- Marine environment data: `{lat, lon, timestamp, wind_speed, wind_direction, current_speed, current_direction}`
- Geospatial reference layers: static GeoJSON files loaded once at startup (coastlines, protected zones, EEZ boundaries) — not streamed, just loaded into memory/Postgres as reference tables

**Output (published to Redis Streams + written to Postgres):**
- `ais.clean` → same AIS shape, but validated, deduplicated, with a normalized ISO-8601 timestamp and a `quality_flag`
- `sar.clean` → `{tile_id, sensor, capture_timestamp, bbox, file_path, polarization, georeferenced: true}`
- `optical.clean` → same shape as sar.clean
- `env.clean` → same env shape, gridded/interpolated onto a common lat/lon grid

**Step-by-step logic:**
1. **Validation** — reject records missing required fields (mmsi, lat, lon, timestamp for AIS; bbox + timestamp for imagery); reject lat/lon outside valid ranges or outside your area of interest bounding box.
2. **Cleaning** — clip speed values to physically plausible ranges (e.g., 0–40 knots for AIS; anything higher is a sensor glitch, flag it rather than silently drop it).
3. **Deduplication** — for AIS, dedupe on (mmsi, timestamp rounded to nearest second); for imagery, dedupe on tile_id.
4. **Time synchronization** — normalize every timestamp to UTC ISO-8601; if downstream services expect a fixed time step (e.g., 1-minute AIS resolution), resample/interpolate positions between reported pings using linear interpolation on lat/lon.
5. **Geo-referencing** — for imagery, confirm/attach the correct coordinate reference system (CRS) and bounding box so later pixel-to-lat/lon conversion is trivial; for AIS, nothing extra needed since it's already lat/lon.
6. Publish each cleaned record to its output stream and insert into the corresponding Postgres table.

**How to build it:**
- FastAPI app with a background worker using `redis.asyncio` (or plain `redis-py` in a thread) to consume `raw.ais` etc.
- Use `rasterio` to open GeoTIFFs and confirm CRS/bbox for SAR/optical.
- Use `geopandas`/`shapely` to load the static GeoJSON reference layers once at startup into a `reference_layers` Postgres table (PostGIS geometry column) — every other service can now do spatial joins against this table instead of re-parsing GeoJSON.
- Write unit tests with a handful of deliberately malformed sample records (missing field, bad timestamp, out-of-range lat/lon) to prove the validation step actually rejects them — this is a very fast, very convincing thing to show judges.
- **Build order:** schema/tables first → validation function → dedup function → time sync function → wire to Redis consume/publish loop → test with the simulator feeding it real sample data.

---

### A2. AIS Analytics Service

**What's happening:** Turns a raw stream of position pings per vessel into a behavioral fingerprint — the numbers that let downstream services say "this vessel is behaving strangely" or "this vessel's track matches the drift model."

**Input:** `ais.clean` stream (per-ping records)

**Output:** `vessel_features` table + `ais.features` stream — one row per (vessel, time-window), e.g. every 15 minutes: `{mmsi, window_start, window_end, avg_speed, speed_variance, course_variance, heading_change_rate, loitering_score, distance_traveled_km, proximity_events: [mmsi list within 1km]}`

**Step-by-step logic:**
1. Buffer incoming pings per vessel into a rolling time window (e.g., last 15 minutes) — keep this in Redis as a sorted set keyed by mmsi, scored by timestamp, so you don't need to re-query Postgres every ping.
2. At each window close, compute: average speed, speed variance, course variance (circular variance, since course wraps at 360°), rate of heading change, a **loitering score** (high when speed is near-zero for a sustained period while not at a known port/anchorage), and total distance traveled.
3. Compute **proximity events**: for the vessel's positions in this window, spatially join against all other vessels' positions in the same window within a distance threshold (this feeds STS detection later — reuse this computation, don't duplicate it).
4. Write the feature row, publish to `ais.features`.

**How to build it:**
- Use `numpy` for the statistics; circular variance for course needs `scipy.stats.circvar` or a manual sin/cos-based computation — worth doing correctly since naive variance on a 0–360 value breaks near the 0/360 boundary.
- Loitering score: a simple heuristic (fraction of time in-window with speed < 1 knot, penalized if within a known port polygon from the reference layer, since loitering near a port is normal and loitering mid-ocean is not) is good enough — don't reach for an ML model here.
- Proximity: since vessel counts are small for a hackathon demo (tens, not thousands), a naive O(n²) distance check per window is completely fine — don't build a spatial index unless you have spare time.
- **Build order:** rolling window buffer → per-window stat functions → proximity join → publish/write.

---

### A3. Anomaly Detection Service

**What's happening:** Consumes the behavioral fingerprints from A2 and decides which ones are abnormal enough to flag.

**Input:** `ais.features` stream

**Output:** `anomaly_events` table — `{mmsi, window_start, anomaly_type: sudden_stop | erratic_course | speed_anomaly | route_deviation, severity: LOW/MEDIUM/HIGH, evidence: {...raw feature values that triggered it}}`

**Step-by-step logic:**
1. Apply simple, explainable rule thresholds first (these alone are demo-worthy): speed drop >80% within one window with no port nearby → `sudden_stop`; course_variance above a fixed threshold → `erratic_course`; speed above the max plausible for that vessel_type → `speed_anomaly`.
2. Optionally layer a statistical check on top: maintain a running mean/std per vessel (or per vessel_type as a fallback for new vessels) and flag features more than ~2.5 standard deviations out — this is a legitimate "anomaly detection" claim without needing a trained model.
3. Write and publish flagged events only (don't publish "nothing anomalous" records).

**How to build it:**
- Pure Python/`numpy`, no ML framework needed. If you want to say "Isolation Forest" for judge appeal and have spare time on Day 2 evening, scikit-learn's `IsolationForest` fit on the feature table is a 20-line addition — but the rule-based version is what should exist first and what you fall back to if time runs out.
- **Build order:** threshold rules → running-stats z-score layer (optional) → event writer.

---

### A4. Dark Vessel Detection Service

**What's happening:** This is where satellite evidence and AIS evidence get cross-checked for the first time. If a satellite image shows a vessel-shaped object at a location and no AIS broadcast explains it, that vessel is "dark" — either AIS is off, broken, or deliberately disabled.

**Input:** `sar.clean`/`optical.clean` (needs vessel detections extracted from the imagery — see note below) + `ais.clean` (all AIS positions within the same time window)

**Note on vessel detection from imagery:** extracting "vessel-shaped bright points" from SAR is itself a small CV task — bright, compact, high-backscatter blobs against darker water background. For a 3-day build, a simple threshold + connected-component analysis (`scipy.ndimage.label` on a backscatter-intensity threshold mask) is sufficient; don't attempt a trained ship-detection CNN unless someone on your team has one ready to go.

**Output:** `dark_vessel_events` — `{detection_id, lat, lon, timestamp, image_source, sensor, confidence, matched_ais: null}`

**Step-by-step logic:**
1. Extract candidate vessel point detections from each SAR/optical tile (bright blob centroids above the backscatter threshold, filtered by plausible vessel size in pixels given the tile's ground resolution).
2. For each detection, spatial-temporal join against `ais.clean`: is there an AIS ping within a distance tolerance (e.g., 500m–1km, wider for lower-resolution imagery) and time tolerance (e.g., ±10 minutes of the image capture time)?
3. If no match found → publish as a `dark_vessel_event` with confidence based on detection strength (blob intensity/size clarity).
4. If matched → discard (this was just a normal, AIS-broadcasting vessel, not dark).

**How to build it:**
- `rasterio` to read the GeoTIFF and pixel array; `numpy` thresholding; `scipy.ndimage.label` + `regionprops` (from `skimage.measure`) to get blob centroids, sizes, and convert pixel coordinates to lat/lon using the tile's affine transform (`rasterio` gives you this transform directly — this is the whole reason A1 confirmed georeferencing).
- The AIS join is a straightforward distance calculation (haversine) between each detection and each AIS ping in the time window — again, small enough scale for a hackathon that a naive loop is fine.
- **Build order:** blob detection on a single sample SAR tile first (get this working and visually verify it against the actual image before wiring anything else) → pixel-to-latlon conversion → AIS join → event publishing.

---

### A5. AIS Spoof Detection Service (Innovation 2)

**What's happening:** For every vessel that IS broadcasting AIS, this service asks "do I believe what it's telling me?" by comparing its claimed position against independent satellite observation of that same vessel.

**Input:** `ais.clean` + satellite vessel detections that DID get matched to an AIS ping in A4 (i.e., the "matched" branch A4 discards — redirect that branch here instead of throwing it away)

**Output:** `ais_trust_scores` — `{mmsi, timestamp, ais_claimed_position, satellite_observed_position, discrepancy_distance_m, trust_score: 0-1, flag: spoofing_suspected if trust_score < 0.5}`

**Step-by-step logic:**
1. For each AIS-satellite matched pair, compute the great-circle distance between the AIS-claimed lat/lon and the satellite-observed lat/lon.
2. `trust_score = max(0, 1 - discrepancy_distance / max_plausible_error)` where `max_plausible_error` accounts for normal AIS/satellite timing and resolution error (a few hundred meters is normal; a few kilometers is not).
3. Also fold in secondary signals if time allows: implausible speed jumps between consecutive AIS pings (teleportation), or an MMSI that briefly changes vessel_name/type mid-track (identity spoofing) — flag these directly regardless of distance discrepancy.
4. Maintain a rolling trust_score per vessel (e.g., exponential moving average) — this rolling value is what Evidence Fusion (C1) and Vessel Risk (A7) actually consume, not a single instantaneous score.

**How to build it:**
- Straightforward haversine distance + simple EMA update — no ML needed. This service is intentionally simple; its value is in *what it enables downstream* (down-weighting untrustworthy AIS evidence), not in algorithmic sophistication.
- **Build order:** distance calc → trust score formula → rolling average → write/publish. This is one of your fastest services to build — do it early on Day 2 so C1 (Fusion) can start integrating against it immediately.

---

### A6. STS (Ship-to-Ship) Detection Service

**What's happening:** Flags two vessels that sit close together, moving slowly, for an extended time — the classic pattern of an illegal at-sea transfer.

**Input:** `ais.features` (reuse the proximity_events computed in A2 — don't recompute distances from scratch)

**Output:** `sts_events` — `{vessel_a, vessel_b, start_time, end_time, duration_minutes, avg_distance_m, avg_combined_speed_knots, confidence}`

**Step-by-step logic:**
1. From consecutive AIS-feature windows, find (vessel_a, vessel_b) pairs that appear in each other's proximity_events across multiple consecutive windows.
2. If the pair stays within threshold distance for longer than a duration threshold (e.g., >30 minutes) AND both vessels' average speed is near-zero → emit an STS event.
3. Confidence scales with duration and how close/how slow — longer and slower = higher confidence.

**How to build it:**
- This is mostly a state-machine over A2's output: track "currently paired" vessel pairs, extend duration while conditions hold, close out and emit the event when they stop holding. A simple in-memory dict (or a Redis hash) keyed by the sorted (vessel_a, vessel_b) tuple works fine.
- **Build order:** last priority in Module A — build this only after A2–A5 are solid, and cut it first if Day 2 afternoon runs long.

---

### A7. Vessel Risk Engine

**What's happening:** The rollup. Every other Module-A service produces a signal about one vessel; this service combines them into one number authorities (and your own pipeline) can act on.

**Input:** `anomaly_events`, `dark_vessel_events` (joined to a vessel where possible via nearest-track match), `ais_trust_scores`, `sts_events`, static vessel metadata (type, flag, history if you have any)

**Output:** `vessel_risk_scores` — `{mmsi, risk_score: 0-100, tier: LOW/MEDIUM/HIGH, contributing_factors: [...], recommended_action: monitor | satellite_task}`; on MEDIUM/HIGH, also publish a `satellite_tasking_request` event

**Step-by-step logic:**
1. Build a **weighted scorecard**, not a black box: e.g., `risk = 30*anomaly_present + 25*(1 - trust_score) + 25*dark_vessel_flag + 20*sts_involvement`, clipped to 0–100. Exact weights don't matter much for a demo — what matters is that you can show the breakdown ("this vessel scored HIGH because trust_score was 0.31 and it had 2 anomaly events") on the dashboard. Explainability is your selling point here, not accuracy.
2. Map score → tier via fixed cutoffs (e.g., <30 LOW, 30–70 MEDIUM, >70 HIGH).
3. MEDIUM/HIGH tier → emit `satellite_tasking_request` (in a real system this would trigger a tasking order to a satellite operator; in your demo this can simply be a dashboard alert/log entry — you don't need to simulate an actual satellite tasking API).

**How to build it:**
- Pure aggregation logic, no ML. Store `contributing_factors` as a JSON blob alongside the score so the dashboard can render "why" in one query — this is a cheap, high-impact feature for your demo.
- **Build order:** last in Module A, once A2–A6 all have at least one real output to aggregate.

---

## MODULE B — Satellite Spill Intelligence

### B1. SAR Spill Processing (Satellite Spill Intelligence Engine)

**What's happening:** Raw SAR imagery is full of speckle noise and needs calibration before "dark regions" (potential oil, since oil dampens capillary waves and lowers radar backscatter) can be reliably extracted as candidate polygons.

**Input:** `sar.clean` (GeoTIFF file path + metadata)

**Output:** `spill_candidates` — `{candidate_id, tile_id, timestamp, polygon (WKT/GeoJSON), area_km2, texture_features: {contrast, homogeneity, edge_sharpness, boundary_irregularity}, mean_backscatter}`

**Step-by-step logic:**
1. **Radiometric calibration** — convert raw digital numbers to sigma-nought (backscatter coefficient) using the metadata calibration constants that come with most SAR products; if your sample dataset is already calibrated (many Kaggle SAR datasets are pre-processed), you can skip/lightly touch this step — check your dataset first before overbuilding this.
2. **Speckle noise filtering** — apply a Lee filter or simple median filter (`scipy.ndimage.median_filter`) to smooth speckle without destroying slick edges.
3. **Dark-region segmentation** — threshold the filtered backscatter image (dark = low backscatter = candidate oil/look-alike); Otsu's method (`skimage.filters.threshold_otsu`) is a fast, defensible automatic threshold choice.
4. **Candidate polygon extraction** — connected-component labeling on the dark mask (`skimage.measure.label` + `regionprops`), filter out components below a minimum area (noise) or with implausible shape (single-pixel artifacts), convert surviving component boundaries to lat/lon polygons using the tile's affine transform, compute area in km².
5. Compute texture features per candidate for B2 to consume: contrast/homogeneity via a gray-level co-occurrence matrix (`skimage.feature.graycomatrix`/`graycoprops`), edge sharpness (gradient magnitude at the boundary), boundary irregularity (perimeter² / area, a standard shape-complexity ratio).

**How to build it:**
- `rasterio` for I/O, `numpy`/`scipy.ndimage` for filtering, `skimage` for thresholding, labeling, region properties, and texture features. This is the most image-processing-heavy service — assign your strongest CV person here and get a single tile working end-to-end (visualize the mask, visualize the extracted polygons overlaid on the image) before scaling to the full dataset.
- **Build order:** load + calibrate one tile → filter → threshold → visualize the mask (sanity check with your own eyes before automating further) → connected components → polygon + texture extraction → wire to Redis loop for all tiles.

---

### B2. Look-Alike Engine (Innovation 3)

**What's happening:** Not every dark SAR region is oil. Low wind areas and biogenic slicks (algae, plankton) also dampen backscatter and look identical to oil in a single image. This service is what separates a naive "dark spot detector" from a genuinely useful system.

**Input:** `spill_candidates` (with texture_features from B1) + `env.clean` (local wind speed at that location/time) + vessel proximity (nearest vessel distance, from a quick spatial join against `ais.clean`) + temporal persistence (does a dark region appear again at roughly the same location in the next SAR pass, from querying `spill_candidates` history)

**Output:** `classified_spills` — `{candidate_id, oil_prob, biogenic_prob, low_wind_prob, other_prob, final_class, classifier_confidence}`

**Step-by-step logic:**
1. Assemble the feature vector per candidate: texture (contrast, homogeneity, edge sharpness, boundary irregularity from B1), area, local wind speed, distance to nearest vessel track, temporal persistence flag (seen in ≥2 consecutive passes at the same location).
2. Apply domain priors as feature engineering, not just raw ML: oil slicks tend to have sharper, more elongated, current/wind-aligned boundaries and persist temporally; biogenic slicks tend to be more diffuse/patchy and less persistent; low-wind areas tend to be very large, smooth-edged, and correlate almost perfectly with wind_speed below ~3 m/s across a broad area rather than a compact patch.
3. Feed the feature vector into a classifier. **Use a Random Forest or Gradient Boosted Trees (scikit-learn)** — it handles a small, mixed-type feature set well, trains in seconds, and — crucially — gives you feature importances you can show judges as "explainability," which a deep CNN would not for a 3-day build.
4. If your dataset has ground-truth labels (oil/not-oil), train directly on it. If it doesn't, hand-label 30–50 candidates yourselves (fast, and still legitimate) or fall back to a rule-based scoring function using the same features with hand-set thresholds — state clearly in your demo which one you used; a well-reasoned rule-based fallback is completely acceptable for a hackathon and is more defensible than a black-box model trained on 20 unlabeled guesses.

**How to build it:**
- `scikit-learn` `RandomForestClassifier`, trained offline in a notebook, exported via `joblib`/`pickle`, loaded at service startup — do NOT retrain live in the request path.
- **Build order:** get B1's texture features flowing first → assemble a small labeled/hand-labeled training set → train + evaluate offline (confusion matrix, even on a tiny test set, is good enough evidence for a demo) → export model → wire into the service → validate predictions against a few candidates you already know the answer to by eye.

---

## MODULE C — Fusion & Attribution

### C1. Multi-Modal Evidence Fusion Service

**What's happening:** This is where independent evidence streams (SAR classification, AIS trust, spatial/temporal alignment, environmental match) get combined into one confidence number per incident — and where the AIS-spoofing insight from A5 actually pays off, by down-weighting evidence from vessels whose AIS you don't trust.

**Input:** `classified_spills` (from B2), `ais_trust_scores` (from A5), `vessel_risk_scores` (from A7), `env.clean`, plus a spatial-temporal join step performed here: which vessel tracks are near the spill polygon within a relevant time window

**Output:** `fused_incidents` — `{incident_id, spill_polygon, timestamp, evidence_vector: {ais_evidence, sar_evidence, optical_evidence, wind_current_match, spatial_match, temporal_match, ais_reliability}, overall_confidence: 0-1}`

**Step-by-step logic:**
1. Take each `classified_spills` record with `final_class = oil` above a minimum confidence — promote it to an `incident` candidate.
2. **sar_evidence** = the B2 `oil_prob` directly.
3. **spatial_match / temporal_match** = how well the wind/current-predicted drift direction from the spill's apparent shape/orientation aligns with actual local wind/current data at that time (an elongated slick oriented with the wind is stronger evidence of a real, actively-drifting slick than a static blob).
4. **ais_evidence** = derived from whether any vessel track passes near the spill polygon around the relevant time — presence of a plausible source vessel nearby is itself corroborating evidence.
5. **ais_reliability** = pulled directly from A5's trust score for whichever vessel is closest/most relevant — **this is the key fusion step**: if that vessel's AIS trust is low, you explicitly reduce confidence in the AIS-derived evidence component even though the SAR evidence stands on its own.
6. Combine into `overall_confidence` via a weighted sum (weights sum to 1; SAR evidence should carry the largest weight since it's your primary detection signal, with AIS/environmental as corroborating signals) — again keep this explainable/weighted, not an opaque model, since your dashboard should be able to show the breakdown.

**How to build it:**
- This service is glue logic + one spatial join (vessel tracks within N km of the spill polygon centroid, within a time window) — use `shapely`/`geopandas` for the polygon/distance operations, plain weighted arithmetic for fusion.
- **Build order:** get the spatial join working first with dummy weights → validate the join finds the right nearby vessels on a hand-checked example → tune weights → wire to real B2/A5/A7 outputs.

---

### C2. Source Attribution Engine (Innovation 1)

**What's happening:** Your headline feature. Given a confirmed spill, this works backward through time and physics to estimate where and when the oil was originally released, then checks which nearby vessel's actual track best explains that estimated origin.

**Input:** `fused_incidents` (spill polygon + timestamp), all AIS tracks within a search radius (e.g., 20km) and a look-back time window (e.g., 12–24 hours before spill detection), `ais_trust_scores`, `env.clean` (wind/current fields for the same time window)

**Output:** `attribution_results` — ranked list per incident: `{incident_id, mmsi, ais_behavior_score, spatial_match, temporal_match, drift_compatibility, vessel_type_score, ais_reliability, final_attribution_probability}` (probabilities normalized to sum to 100% across all candidates + an explicit "Unknown" bucket)

**Step-by-step logic:**
1. **Backward particle tracking:** take the observed spill polygon, seed particles across it, and advect them *backward* in time (negate the wind/current vector field) step by step until reaching the look-back horizon — this reuses the exact same physics as the forward drift model in D1, just run in reverse; the result is an estimated origin region + estimated origin time.
2. For each candidate vessel with a track passing through the search radius/time window, compute:
   - **spatial_match** — how close the vessel's position (at the estimated origin time) is to the estimated origin region.
   - **temporal_match** — how well the vessel's presence window overlaps the estimated origin time.
   - **drift_compatibility** — re-run the *forward* drift model starting from the vessel's actual position/time and check how well the resulting predicted polygon overlaps the actually-observed spill polygon (this is a strong, physically-grounded check, distinct from simple proximity).
   - **vessel_type_score** — a prior based on vessel type (tankers/cargo carrying fuel are inherently more likely sources than a small fishing vessel — use a simple hand-set prior table, not learned).
   - **ais_behavior_score** — pull any A3 anomaly flags for this vessel in the relevant window (evasive/erratic behavior right after the estimated origin time is corroborating).
   - **ais_reliability** — from A5, same value as used in C1.
3. Combine these into a single per-vessel score (weighted sum, same explainable-scorecard philosophy as everywhere else), then **normalize across all candidate vessels** so the scores sum to 100%, reserving some probability mass for "Unknown" if no candidate scores strongly (never force a 100% confident false attribution just because normalization requires the numbers to sum to something).

**How to build it:**
- The backward particle model is literally D1's forward model with the wind/current vectors negated — build D1 first (or in parallel, same person/pair if possible) and share the advection function between both services, since they are the same physics run in opposite time directions.
- Particle seeding/advection: represent the spill polygon boundary as N sample points (`shapely` polygon exterior coordinates, resampled to a fixed point count), step each point backward using `position -= (wind_drift_factor * wind_vector + current_vector) * dt` for each discrete time step, reconstruct the polygon from the moved points at each step via convex hull or alpha-shape (`shapely.geometry.MultiPoint(...).convex_hull` is the fast, good-enough option for a hackathon).
- **Build order:** shared advection function (build and test standalone first — feed it a known start point and known constant wind, verify it moves the point the expected distance/direction) → wire forward version into D1 → wire backward version into C2 → candidate vessel scoring → normalization. This is your most complex service — protect Day 3 morning time for it specifically and don't let it get squeezed by dashboard polish.

---

## MODULE D — Forecasting & Severity

### D1. Spill Drift & Forecast Engine (Innovation 7)

**What's happening:** Physically simulates how the spill polygon will move and spread forward in time, using the same particle-advection idea as C2 but running forward.

**Input:** `fused_incidents` (current spill polygon + timestamp) + `env.clean` (wind/current vector fields over the forecast horizon — for a hackathon, holding wind/current constant at their most recent observed value for the whole forecast window is a completely acceptable simplification; state this simplification explicitly to judges)

**Output:** `drift_forecasts` — one record per horizon per incident: `{incident_id, horizon: 6h/12h/24h/48h/72h, predicted_polygon, centroid_distance_km, confidence: decays with horizon}`

**Step-by-step logic:**
1. Seed particles across the current spill polygon (same resampling approach as C2).
2. For each time step up to each horizon, advect every particle forward: `position += (0.03 * wind_vector + current_vector) * dt` — the 0.03 (3%) wind-drift factor is the standard oceanographic rule-of-thumb for how much of the wind vector contributes to surface slick movement; current contributes roughly at full vector strength.
3. At each checkpoint (6h, 12h, 24h, 48h, 72h) snapshot the particle cloud, reconstruct a polygon (convex hull or alpha-shape), record it as that horizon's forecast.
4. Confidence should decrease with horizon (real drift forecasts get less reliable further out) — a simple decay function like `confidence = max(0.2, 1 - 0.1*horizon_hours/6)` is defensible and easy to explain.

**How to build it:** shares the advection core with C2 — build once, use in both directions. `numpy` for the particle position arrays (vectorize the advection step across all particles at once rather than looping in Python), `shapely`/`geopandas` for polygon reconstruction and area/distance calculations.

**Build order:** get a single forward advection step working and visually sane on one test case (plot the polygon before/after and eyeball that it moved in the expected wind direction) → loop across all five horizons → wire to real fused_incidents data → this unlocks C2 immediately after, since C2 is the same function run backward.

---

### D2. Spill Severity & Impact Service (Innovation 6)

**What's happening:** Converts the raw spill detection + forecast into a decision-relevant severity classification.

**Input:** `fused_incidents` (spill polygon), `drift_forecasts` (growth trend across horizons), static reference layers (coastline, protected zones from A1)

**Output:** `severity_assessments` — `{incident_id, severity: LOW/MEDIUM/HIGH/CRITICAL, estimated_area_km2, estimated_volume_range_tonnes: [low, high], growth_rate_pct_per_hr, coastline_distance_km, ecological_exposure: LOW/MEDIUM/HIGH}`

**Step-by-step logic:**
1. **Area** — directly from the spill polygon (`shapely` `.area`, converted to km² accounting for projection).
2. **Growth rate** — compare polygon area at the 6h forecast vs. current area, express as %/hour.
3. **Volume range** — do NOT compute a single number. Use a published thickness-class heuristic: classify the slick by its mean backscatter intensity (from B1) into thin sheen (~0.1–1 µm thickness) vs. moderate (~1–10 µm) vs. thick (~10–100 µm) per unit area, multiply the classified thickness range by area to get a volume **range** in tonnes — always report as a range, and say explicitly in your write-up that this is a documented limitation of satellite-based spill quantification, not a gap in your system.
4. **Coastline distance** — nearest-distance spatial query from the spill polygon centroid to the coastline reference layer (PostGIS `ST_Distance` if you loaded coastline into PostGIS, or `shapely`/`geopandas` `.distance()` if kept in memory).
5. **Ecological exposure** — spatial intersection/proximity check against protected-zone/mangrove/fishing-zone reference layers → LOW/MEDIUM/HIGH based on distance bands.
6. **Overall severity** — combine area, growth rate, coastline distance, and ecological exposure into one tier via a simple decision table (e.g., large + fast-growing + close to coast + high ecological exposure = CRITICAL) — again, an explainable rule table beats a trained model here.

**How to build it:** mostly geospatial queries (PostGIS if your reference layers are loaded there, which is the recommended path since you're already running Postgres+PostGIS) plus straightforward heuristic tables. No ML.

**Build order:** area + coastline distance first (simplest, immediately demoable) → growth rate (depends on D1 being done) → ecological exposure → volume range (do this last, it's the least critical number and the one most likely to need a caveat anyway) → severity tier combination.

---

## MODULE E — Decision & Command

### E1. Response Decision Engine (Innovation 5)

**What's happening:** The final translation from "here's what we detected" to "here's what to actually do about it" — this is what makes the system decision-support rather than just a detector.

**Input:** `fused_incidents`, `attribution_results` (top candidate + confidence), `severity_assessments`, `drift_forecasts`

**Output:** `response_recommendations` — `{incident_id, priority_actions: [ordered strings], generated_at}`

**Step-by-step logic:**
1. Apply a decision tree, roughly: if severity is CRITICAL/HIGH → "Deploy containment" is always action #1; if coastline ETA (from the forecast horizon where predicted polygon first intersects the coastline buffer) is under some threshold → "Notify coastal authority" ranks high; if ecological_exposure is HIGH → "Monitor [fishing zone/mangrove name]" is included; if attribution confidence for the top vessel is above a threshold → "Track probable source vessel [mmsi]" is included, otherwise omit that action or replace it with "Continue attribution monitoring."
2. Keep the whole tree in one readable function/config table — you want to be able to point at exactly which input triggered exactly which recommendation when a judge asks "why did it say that."

**How to build it:** pure rule-based branching logic, trivial to implement, but write it as a clean, explicit decision table (e.g., a small YAML/JSON config mapping condition combinations to actions) rather than deeply nested if/else — this makes it easy to demo, easy to tweak live if a judge asks "what if severity were lower," and easy for a teammate other than the author to modify under time pressure.

**Build order:** last service to build in the whole system, since it only needs to combine outputs everyone else already produced — don't start this before C2/D2 have real data flowing.

---

### E2. API Gateway

**What's happening:** The dashboard should never talk to 13 different services directly. The gateway is the one stable contract the frontend team builds against, so backend services can keep changing internally without breaking the UI.

**Input:** HTTP requests from the dashboard (e.g., `GET /incidents`, `GET /incidents/{id}`, `GET /vessels`, `GET /vessels/{mmsi}/risk`) + it also subscribes to the relevant Redis Streams itself so it can push live updates

**Output:** aggregated JSON responses to the dashboard; a WebSocket or Server-Sent-Events channel (`/live`) that pushes events (new incident, risk tier change, new forecast) as they happen

**How to build it:** FastAPI, with each REST endpoint simply querying the relevant Postgres table (joins across `fused_incidents`, `attribution_results`, `severity_assessments`, `response_recommendations` for the incident detail endpoint) — this service does almost no computation of its own, it's a read/aggregation layer plus a WebSocket relay. Build this early (Day 1 afternoon) as a skeleton with mock/stub data so the dashboard team is never blocked waiting on backend services.

---

### E3. Authority Command Dashboard

**What's happening:** The single screen judges will actually watch. It needs to make every one of your six innovations visible and understandable in under a minute of looking at it.

**Input:** REST calls + WebSocket stream from the Gateway

**Output:** rendered UI —
- **Live map** (Leaflet or Mapbox GL): vessel markers (colored by risk tier from A7), spill polygons (colored/labeled by severity from D2), dark-vessel markers (distinct icon, from A4), a forecast **timeline slider** that swaps the displayed spill polygon between the 6h/12h/24h/48h/72h forecasts from D1.
- **Incident detail panel**: incident ID, risk level, source vessel + attribution confidence (the "Vessel A 87% / B 8% / C 3% / Unknown 2%" breakdown from C2 — render this as a simple horizontal bar list, it's an extremely clear visual), spill area, predicted spread, recommended actions from E1 in order.
- **Alert feed**: a scrolling/live-updating list driven by the WebSocket channel — new incidents, risk escalations, dark vessel sightings appear here in real time as your simulator plays them in.

**How to build it:** React + Leaflet (simpler license/setup than Mapbox for a hackathon) + a lightweight state manager (React Query for the REST calls, a simple `useEffect`+`WebSocket` hook for live updates — no need for Redux at this scale). Build the map and static incident panel first against mock JSON (don't wait on the Gateway/backend), then swap the mock data source for the real Gateway calls once available — this lets frontend and backend work fully in parallel from Day 1.

---

## MODULE F — Infrastructure

### Simulator / Data Replay

**What's happening:** Not a "service" in the intelligence sense — its whole job is to make your static sample datasets look like a live operational feed, which is what makes the demo compelling instead of static.

**Input:** the sample SAR tiles, sample AIS track file, sample env data sitting in `/data`

**Output:** pushes records onto `raw.ais` (paced out over time, e.g., replaying a day of AIS history compressed into 2–3 minutes) and drops SAR tile references onto whatever trigger A1 listens for, at scripted intervals timed to match your demo narrative (e.g., the "dark vessel" and the "spill" appear a few minutes into the demo, not at second zero, so you have time to narrate the clean state first)

**How to build it:** a standalone Python script (runs as its own container, no FastAPI needed) that reads the sample files, sorts by timestamp, and pushes to Redis with `asyncio.sleep()` calls scaled down from real elapsed time to demo-friendly elapsed time. Make the playback speed a config value — you'll want to run it at full speed while debugging and at "demo speed" during the actual presentation.

### Postgres + PostGIS / Redis

Standard containers from official images (`postgis/postgis`, `redis:7`) — no custom build needed beyond an `init.sql` that creates your schema/tables and enables the PostGIS extension, and a `redis.conf` if you want persistence enabled (not required for a demo — an in-memory Redis that resets on restart is fine).

---

## Build Order Summary (cross-service dependency order)

If you only remember one thing from this document, remember this dependency chain — it tells you the true order services must come online, regardless of which team member owns which module:

1. Infra (Postgres/PostGIS + Redis containers) + schema
2. A1 Ingestion (everything depends on this)
3. A2 AIS Analytics → A3 Anomaly, A5 Spoof, A6 STS (all depend on A2's features) → A4 Dark Vessel (depends on A1's imagery + AIS) → A7 Risk Engine (depends on A2–A6)
4. B1 SAR Processing → B2 Look-Alike Engine (depends on B1's texture features)
5. D1 Drift Forecast (build this before or alongside C2, since C2 reuses its physics)
6. C1 Evidence Fusion (depends on B2, A5, A7) → C2 Source Attribution (depends on C1's incidents + D1's advection function)
7. D2 Severity (depends on C1's incidents + D1's forecasts)
8. E1 Response Decision (depends on C1, C2, D2, D1 — build last)
9. E2 Gateway (build a stub early, wire to real data late) + E3 Dashboard (build against mocks early, wire to Gateway late)

Everything in steps 2–7 can be parallelized across team members once step 1–2 are done; steps 8–9 are necessarily last since they consume everyone else's output.
