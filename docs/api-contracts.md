# API Contracts

## Redis Streams

### `spill.candidates.raw`

| Field | Value |
|---|---|
| Stream | `spill.candidates.raw` |
| Producer | `sar-spill-intelligence` worker (`services/sar-spill-intelligence/app/worker.py`) |
| Consumer group | none assigned yet (downstream classification service to follow) |
| Purpose | Announces the raw SAR spill candidates persisted to the `spill_candidates` PostGIS table for one scene, after morphology + polygonization (Step 3). Downstream look-alike classification consumes these candidate IDs. |

One message is emitted **per scene**, **only after** every candidate row for that scene has been written to PostGIS, so consumers never receive candidate IDs that were not persisted.

**Message structure (flat fields, JSON-encoded values):**

```json
{
  "scene_id": "COPERNICUS/S1_GRD/S1A_IW_GRDH_1SDV_20240101T004523_20240101T004548_...",
  "acquisition_time": "2024-01-01T00:45:23.000Z",
  "candidate_ids": ["<uuid>", "<uuid>"],
  "orbit": "...",
  "polarization": "...",
  "resolution": "..."
}
```

**Required fields:**

- `scene_id` — the SAR scene id from the original `sar.clean` `scene_metadata`.
- `acquisition_time` — scene acquisition time (ISO-8601).
- `candidate_ids` — UUIDs of the `spill_candidates.candidate_id` rows persisted for this scene.

**Scene metadata:** `orbit`, `polarization`, and `resolution` are forwarded from `sar.clean` `scene_metadata` when present (optional fields).

**Candidate status semantics:** `spill_candidates.status` is `raw` for every candidate announced here. `raw` means "unvalidated SAR detection output" — these have not yet been through look-alike/classification filtering. Downstream promotion (e.g., to a confirmed spill incident) is a later stage.

## Lookalike classification statuses (Step 4)

The `spill_candidate_status_enum` is extended (idempotently) so candidate rows can be updated in place by the lookalike-engine without deleting or duplicating them. The complete set of statuses is:

| Status | Meaning |
|---|---|
| `raw` | Fresh SAR detection from Step 3; not yet classified. |
| `likely_ship_shadow` | Rejected by the ship-shadow heuristic (elongated dark region adjacent to a bright high-backscatter target). |
| `likely_calm_water` | Rejected by the calm-water heuristic (large, internally uniform, diffuse dark region). |
| `possible_slick` | Not rejected by the implemented lookalike heuristics. This does **not** mean confirmed oil: it only means the candidate remains under consideration. |

`possible_slick` is never described as `confirmed`, `confirmed_spill`, or `oil_confirmed` anywhere in the API or schema.

No new Redis stream is introduced by Step 4: the lookalike-engine consumes `spill.candidates.raw` (consumer group `lookalike-engine`) and writes classifications back to `spill_candidates.status`. Required per-candidate pixel artifacts (dark-mask region, SAR intensity region, and a bright-target mask) are expected to come from the storage/reference mechanism provided by Step 3; they are not reconstructed inside the lookalike service.

## Scene artifact reference (processed SAR raster)

Raster pixels are **never** stored in PostgreSQL. For each processed scene, the SAR worker writes one artifact bundle to a shared Docker volume (`sar-scene-artifacts`, mounted at `SAR_ARTIFACT_ROOT`, default `/data/artifacts`), retrievable by `scene_id`:

```text
<SAR_ARTIFACT_ROOT>/<scene_id sanitized>/
    metadata.json             affine transform, raster shape, crs, scene_id
    raw_image.npy             pre-despeckle backscatter values (float)
    filtered_image.npy         Lee-filtered backscatter values (float)
    cleaned_mask.npy           morphologically cleaned dark-candidate mask (bool)
    bright_target_mask.npy     high-backscatter targets (bool)
```

Mapping: `candidate_id -> scene_id -> scene artifact` (no DB index required; the sanitized `scene_id` directory name is the key). No raster columns are added to `spill_candidates`.

**Pixel coordinate mapping:** world X (longitude) maps to the column axis and world Y (latitude) to the row axis via the inverse of the stored affine transform; crop regions are padded for the ship-shadow adjacency heuristic and clamped to the raster extent.

**Raw SAR dependency (Step 5):** GLCM texture features are computed from `raw_image.npy` (pre-despeckle values). The Lee filter (`filtered_image.npy`) partially removes texture detail and is never used for texture analysis.

**Required configuration (values come from validation data, never hardcoded):**

- `SAR_MIN_AREA_M2` — physical minimum spill area. Unset/invalid fails SAR Step 3 clearly.
- `SAR_BRIGHT_TARGET_THRESHOLD` — high-backscatter threshold used to derive the bright-target mask (`filtered_image > threshold`). The SAR code has no existing bright-target detector (CFAR detects dark targets only), so this configurable threshold must be set from validation data; unset/invalid fails SAR Step 3 clearly.
- `SAR_ARTIFACT_ROOT` — artifact volume root (default `/data/artifacts`).

## Step 5 — GLCM texture + confidence scoring

`possible_slick` candidates that survive Step 4 lookalike rejection are scored
by the lookalike-engine using GLCM texture features (computed from the **raw
pre-despeckle** scene artifact) and a heuristic confidence score.

Additional `spill_candidates` columns:

| Column | Type | Meaning |
|---|---|---|
| `confidence` | DOUBLE PRECISION (0..1) | Step 5 heuristic confidence. |
| `classification_label` | `spill_candidate_status_enum` | Final Step 4/5 label. |
| `texture_features` | JSONB | GLCM texture features (contrast, homogeneity, energy, correlation, mean/std backscatter). |

New labels (added idempotently to `spill_candidate_status_enum`):

| Status | Meaning |
|---|---|
| `possible_oil_spill` | Scored candidate above `CONFIDENCE_THRESHOLD` (0.5). It does **not** mean ground-truth oil confirmation. |
| `low_confidence` | Scored candidate at or below `CONFIDENCE_THRESHOLD`. |

### `spill.candidates.filtered`

| Field | Value |
|---|---|
| Stream | `spill.candidates.filtered` |
| Producer | lookalike-engine worker (Step 5) |
| Consumer group | `evidence-fusion` (Step 6) |
| Purpose | Announces candidates whose Step 5 classification is `possible_oil_spill`; consumed by evidence-fusion to build `incident.fused` events. |

One message per scored candidate (only for `possible_oil_spill`; `low_confidence`
and lookalike-rejected candidates are retained in PostGIS but not published here).

**Message structure (flat fields, JSON-encoded values):**

```json
{
  "candidate_id": "<uuid>",
  "scene_id": "COPERNICUS/S1_GRD/...",
  "confidence": 0.73,
  "classification_label": "possible_oil_spill",
  "acquisition_time": "...",
  "orbit": "...",
  "polarization": "...",
  "resolution": "..."
}
```

Required fields: `candidate_id`, `scene_id`, `confidence`, `classification_label`. Scene metadata (`acquisition_time`, `orbit`, `polarization`, `resolution`) is forwarded from the consuming `spill.candidates.raw` message when present.

**No polygon geometry in the WS envelope.** These events carry only scalar
metadata (ids, confidence, labels, `is_synthetic`, and `correlated_vessel_id`
for `incident.fused`). The dashboard resolves the candidate polygon, `area_m2`,
and `texture_features` via the existing api-gateway endpoint
`GET /spill/candidates/{candidate_id}` (returns GeoJSON `geometry` + full
record), keyed by `candidate_id`. The dashboard never fabricates geometry.

### `incident.fused`

| Field | Value |
|---|---|
| Stream | `incident.fused` |
| Producer | evidence-fusion worker (Step 6) |
| Consumer group | none assigned yet (Step 7 source attribution / downstream to follow) |
| Purpose | Fused SAR evidence + nearby vessel-risk context for a `possible_oil_spill` candidate. Fusion only — never source attribution. |

One message per fused SAR candidate. **Inputs:** `spill.candidates.filtered`
(SAR evidence) + high-risk vessels (tier `HIGH`/`CRITICAL` from
`vessel_risk_scores`) that are geographically and temporally correlated with the
candidate.

**Correlation semantics**

- Spatial: PostGIS geography `ST_DWithin(candidate_centroid, vessel_position, spatial_window_m)` — metres, never degree differences.
- Temporal: vessel position within `[acquisition_time - window, acquisition_time + window]`.
- Windows (tunable, not validated): `EVIDENCE_SPATIAL_WINDOW_M` default `5000` (5 km), `EVIDENCE_TEMPORAL_WINDOW_HOURS` default `6` (6 hours).
- Selection when multiple vessels qualify: nearest geographic distance; ties broken by lowest mmsi (deterministic).
- **`correlated_vessel_id` is contextual evidence** that a vessel-risk record was spatially/temporally correlated with the candidate. It does **not** mean the vessel caused the spill.

**Message structure (flat fields, JSON-encoded values):**

```json
{
  "candidate_id": "<uuid>",
  "scene_id": "COPERNICUS/S1_GRD/...",
  "confidence": 0.73,
  "classification_label": "possible_oil_spill",
  "acquisition_time": "...",
  "orbit": "...",
  "polarization": "...",
  "resolution": "...",
  "correlated_vessel_id": 42,
  "correlated_vessel": {
    "mmsi": "123456789",
    "risk_score": 80.0,
    "tier": "HIGH",
    "recommended_action": "satellite_task",
    "distance_m": 800.0,
    "position_timestamp": "..."
  }
}
```

`correlated_vessel` and `correlated_vessel_id` are `null` when no qualifying
vessel is found; the SAR candidate is still published. No field in this event
represents source attribution (`caused_by` / `responsible_vessel` /
`source_vessel` / `attribution` are never emitted).

## Synthetic demo provenance (Step 7)

`is_synthetic` is an explicit boolean provenance flag that identifies candidates
injected from synthetic/demo data. It is carried unchanged through the entire
pipeline and is never inferred from `scene_id`, filenames, or test metadata.

> `is_synthetic=true` identifies provenance only and does not imply a different
> classification or confidence calculation. Synthetic candidates flow through
> the exact same segmentation, lookalike filtering, GLCM, confidence scoring, and
> evidence-fusion paths as real data.

**How it is set**

- data-ingestion SAR trigger: `inject_synthetic` (explicit field/argument) takes
  precedence over the `INJECT_SYNTHETIC` environment variable; disabled by
  default. Injection writes a new `<stem>_synthetic.tif` (the original raster is
  never overwritten) and stamps `is_synthetic=true` in `sar.clean` metadata.
- Every downstream stage carries the boolean: `spill_candidates.is_synthetic`
  (column, `BOOLEAN NOT NULL DEFAULT FALSE`), `spill.candidates.raw`,
  `spill.candidates.filtered`, and `incident.fused` all include `is_synthetic`.

**Propagation contract**

```text
data-ingestion → sar.clean → spill_candidates → spill.candidates.raw
              → spill.candidates.filtered → incident.fused
```

The value is preserved at every boundary; stages that update rows must not
overwrite it, and evidence-fusion inherits it from the SAR candidate (never from
the vessel-risk record).
