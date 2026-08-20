# SAR Spill Intelligence Pipeline (Steps 1–5)

Implementation notes for the SAR spill-detection pipeline as built in this
repository. This describes what exists today and how the pieces connect.

Companion documents:

- `docs/api-contracts.md` — Redis stream contracts (`spill.candidates.raw`,
  `spill.candidates.filtered`) and artifact layout.
- `docs/spatial.md` — coordinate convention (EPSG:4326, longitude-latitude,
  geography cast for meters).
- `docs/context-files/service-deep-dive-spec.md` — original design intent.

---

## 1. Scope / what this is

An event-driven SAR oil-spill candidate pipeline:

```text
SAR GeoTIFF
   │  (sar.clean, produced upstream by data-ingestion)
   ▼
sar-spill-intelligence  (Steps 1–3)
   ├── Lee despeckle  -> LE    CFAR (small dark targets)
   ├── Otsu dark-region segmentation (large slicks/calm water)
   ├── mask = segmentation ∪ CFAR
   ├── morphology (open 3 / close 5)
   ├── polygonize → spill_candidates (PostGIS) + scene artifact bundle
   ▼
spill.candidates.raw (Redis, one message per scene)
   ▼
lookalike-engine  (Steps 4–5)
   ├── Step 4: shape heuristics -> likely_ship_shadow / likely_calm_water / possible_slick
   ├── Step 5: possible_slick -> GLCM texture (raw SAR) -> confidence score
   ├── store confidence / classification_label / texture_features (PostGIS)
   ▼
spill.candidates.filtered (Redis, only possible_oil_spill)
```

**Design constraints that hold throughout**

- No raster pixels are ever stored in PostgreSQL — a scene artifact bundle on a
  Docker volume (`sar-scene-artifacts`) holds the arrays, keyed by `scene_id`.
- No `confirmed` / `confirmed_oil` / ground-truth label exists anywhere.
  `possible_oil_spill` means only "not rejected and above a heuristic
  threshold".
- Geometry/area math goes through PostGIS geography (`shared.spatial.geo`),
  never degree².
- Rows are updated, never deleted.

---

## 2. Service: sar-spill-intelligence (Steps 1–3)

Located at `services/sar-spill-intelligence/`.

| File | Purpose |
|---|---|
| `app/despeckle.py` | Vectorised Lee-style local-statistics speckle filter (dB input). |
| `app/cfar.py` | Guard-cell CFAR annulus detector for small dark targets (ship shadows). |
| `app/segmentation.py` | `dark_region_mask(image, method="otsu")` — Otsu thresholding over the lower-median backscatter subset (documented equivalent; lower dB = darker). |
| `app/morphology.py` | `clean_mask` (binary opening → closing, optional `min_area_px`), `estimate_min_area_px` (requires explicit `min_area_m2`). |
| `app/polygonize.py` | `extract_candidates` — connected components → GeoJSON Polygons (longitude, latitude), pixel count, centroid. Pure; no DB. |
| `app/worker.py` | Consumes `sar.clean`; applies the chain; resolves area via `area_m2_from_geometry`; writes `spill_candidates`; saves the scene artifact; publishes `spill.candidates.raw`. |

**Candidate mask**

```text
binary_mask = dark_region_mask(filtered) OR cfar_detect(filtered)
```

Both detectors are kept: Otsu segmentation recovers large smooth dark regions
(slicks, calm water), CFAR catches small targets. Neither is deleted; their
union feeds `clean_mask` and then `extract_candidates`.

**Area** — every `area_m2` is computed as
`ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(<polygon>), 4326)::geography)` via the
shared `area_m2_from_geometry` helper.

**Minimum area** — `extract_candidates(..., min_area_m2=SAR_MIN_AREA_M2)`; the
pixel threshold is derived by `estimate_min_area_px` (explicit `min_area_m2`
required, no hidden default).

---

## 3. Service: lookalike-engine (Steps 4–5)

Located at `services/lookalike-engine/`.

| File | Purpose |
|---|---|
| `app/shape_filters.py` | Step 4 pure heuristics: `compute_shape_descriptors`, `is_likely_ship_shadow`, `is_likely_calm_water`, `classify_candidate`. Exactly three labels. |
| `app/texture.py` | Step 5 `compute_glcm_features(raw_image, region_mask, levels=32)` — GLCM on **raw pre-despeckle** values (distances `[1]`, angles `[0, π/4, π/2, 3π/4]`, symmetric + normed; mask-respecting via sentinel level). |
| `app/scoring.py` | Step 5 heuristic scoring with the locked constants (see §6) and `score_candidate(candidate, texture_features, context_score=0.5)`. |
| `app/worker.py` | Consumes `spill.candidates.raw`; Step 4 classify → status; Step 5 score `possible_slick` → store fields → publish `spill.candidates.filtered`. |
| `app/main.py` | FastAPI shell starting the worker; `/health`, `/status`. |

**Step 4 statuses** — `likely_ship_shadow`, `likely_calm_water`,
`possible_slick` (plus `raw` for unclassified). Classification uses the
scene-artifact pixel inputs; ship-shadow requires an actual supplied
bright-target mask (never fabricated from the candidate).

**Step 5 flow**

```text
possible_slick
   ├── retrieve scene artifact -> raw_image.npy (raw, pre-despeckle)
   ├── map geometry -> pixel bbox (inverse affine, lon→col / lat→row, padded)
   ├── crop raw SAR region + candidate dark mask
   ├── compute_glcm_features(raw, dark)          (never filtered_image)
   ├── score_candidate(...) -> confidence + label
   ├── UPDATE confidence / classification_label / texture_features
   │      WHERE status='possible_slick' AND classification_label IS NULL
   └── label == possible_oil_spill -> publish spill.candidates.filtered
```

Ship-shadow / calm-water candidates are passed through (classification_label =
the Step 4 label, no confidence). Nothing is deleted; later statuses are never
overwritten (raw-guard updates only).

---

## 4. Scene artifacts

One bundle per processed scene on the `sar-scene-artifacts` Docker volume
(`SAR_ARTIFACT_ROOT`, default `/data/artifacts`), retrievable by `scene_id`
(`candidate_id -> scene_id -> artifact`):

```text
<SAR_ARTIFACT_ROOT>/<scene_id sanitized>/
    metadata.json            affine transform, shape, crs, scene_id
    raw_image.npy            pre-despeckle backscatter (float)   [Step 5 required]
    filtered_image.npy       Lee-filtered backscatter (float)
    cleaned_mask.npy         final candidate mask (bool)
    bright_target_mask.npy   high-backscatter targets (bool)
```

Implemented in `services/shared/artifacts.py` (save/load, sanitised key,
`geometry_to_pixel_bbox`, `crop_region`). The lookalike worker reads raw —
GLCM **must** use `raw_image.npy` because despeckling partially removes texture
detail; the E2E verified stored texture == raw-crop GLCM and ≠ filtered-crop
GLCM.

---

## 5. Database (`infra/postgres/`)

### `spill_candidates`

```sql
candidate_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4()
scene_id              VARCHAR(255) NOT NULL
acquisition_time      TIMESTAMPTZ NOT NULL
geom                  GEOMETRY(Polygon, 4326) NOT NULL
area_m2               NUMERIC(14,2) NOT NULL CHECK (area_m2 >= 0)
pixel_count           INTEGER NOT NULL CHECK (pixel_count > 0)
status                spill_candidate_status_enum NOT NULL DEFAULT 'raw'
created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
confidence            DOUBLE PRECISION CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
classification_label  spill_candidate_status_enum
texture_features      JSONB
```

Indexes: GIST on `geom`; btree on `scene_id`, `acquisition_time`, `status`.

### `spill_candidate_status_enum`

```text
raw, likely_ship_shadow, likely_calm_water, possible_slick, possible_oil_spill, low_confidence
```

### Migrations (idempotent, aligned with `init.sql`)

| File | Change |
|---|---|
| `migrate_stage2.sql` | Create `spill_candidates` + enum `raw`. |
| `migrate_stage3.sql` | Add `likely_ship_shadow`, `likely_calm_water`, `possible_slick`. |
| `migrate_stage4.sql` | Add `possible_oil_spill`, `low_confidence`; add `confidence`, `classification_label`, `texture_features`. |

All were applied to the compose developer Postgres. Always show the migration +
`init.sql` diff and obtain approval before executing against a database.

---

## 6. Step 5 scoring constants (`app/scoring.py`)

```python
DARKNESS_WEIGHT      = 0.30
TEXTURE_WEIGHT       = 0.20
SHAPE_WEIGHT         = 0.20
AREA_WEIGHT          = 0.10
SHIP_SHADOW_PENALTY  = -0.15
CONTEXT_WEIGHT       = 0.20
CONFIDENCE_THRESHOLD = 0.5
```

Confidence = weighted component sum (+ ship-shadow penalty) clipped to
`[0, 1]`; NaN/inf sanitised. `confidence > threshold` → `possible_oil_spill`,
`<=` → `low_confidence` (pass-through labels keep their Step 4 label).
`context_score` is reserved for the Step 6 evidence-fusion/AIS stage.

---

## 7. Redis contracts

See `docs/api-contracts.md` for the full contracts.

| Stream | Producer | Contents |
|---|---|---|
| `spill.candidates.raw` | sar-spill-intelligence | one message/scene: `scene_id`, `acquisition_time`, `candidate_ids[]`, scene metadata |
| `spill.candidates.filtered` | lookalike-engine | one message/candidate with `classification_label = possible_oil_spill`: `candidate_id`, `scene_id`, `confidence`, `classification_label`, scene metadata |

Both use the shared `publish_to_stream` helper (flat fields, JSON-encoded
values).

---

## 8. Configuration

| Variable | Meaning | Status |
|---|---|---|
| `SAR_MIN_AREA_M2` | Minimum spill area (m²), required by Step 3. | Must come from validation data — **NOT YET VALIDATED** |
| `SAR_BRIGHT_TARGET_THRESHOLD` | High-backscatter threshold for the bright-target mask. | Must come from validation data — **NOT YET VALIDATED** |
| `SAR_ARTIFACT_ROOT` | Artifact volume root (default `/data/artifacts`). | Deployed default |

Unset/invalid `SAR_MIN_AREA_M2` or `SAR_BRIGHT_TARGET_THRESHOLD` fails SAR
processing with a clear configuration error (no silent persistence-free
success). Test fixtures used `SAR_MIN_AREA_M2=20000` / threshold `1.0` and are
**fixture values only**, never committed as production defaults.

---

## 9. Container wiring

- Both services' Dockerfiles merge the canonical top-level `shared/`
  (`spatial/`, `db/`) with the AIS-pipeline shared modules
  (`redis_client.py`, `geo_utils.py`, `models.py`, `artifacts.py`) into one
  `/app/shared` — `services/shared/db.py` is intentionally not copied (would
  collide with the canonical `shared/db/` package).
- The SAR image installs rasterio runtime libraries (`libexpat1 libcurl4
  libpng16-16 libjpeg62-turbo libopenjp2-7 libgeos-c1v5`) needed by the
  rasterio wheel.
- `docker-compose.yml`: `sar-scene-artifacts` named volume mounted at
  `/data/artifacts` in both services; per-module bind mounts for the shared
  package; ports `8008` (SAR) and `8009` (lookalike).

---

## 10. Tests

| Suite | Items | Notes |
|---|---|---|
| SAR (`services/sar-spill-intelligence/test_*`) | CFAR, despeckle, morphology/polygonize, segmentation | `24/24` |
| Lookalike (`services/lookalike-engine/test_*`) | shape filters, artifact mapping, texture, scoring | `35/35` |

Guidelines inherited from the work:

- Tests run in-process; a DB-backed geography-area test skips when PostGIS is
  unreachable (reuse the `tests/test_spatial.py` pattern).
- The two service test dirs both use a package named `app`, so run the suites
  separately: `pytest services/sar-spill-intelligence/` and
  `pytest services/lookalike-engine/`.

---

## 11. End-to-end validation

A deterministic fixture (`scripts/generate_synthetic_sar_fixture.py`, values
are TEST fixtures, not scientific) drives the real Docker pipeline. It produces:

- a large uniform calm-water carpet → `likely_calm_water` (retained, not scored),
- a bright vessel + elongated dark stripe → `likely_ship_shadow` (retained, penalised),
- a compact high-contrast blob → `possible_slick` → scored → `possible_oil_spill`
  (published to `spill.candidates.filtered`).

Verified in the E2E run: `raw_image.npy` identical to the source GeoTIFF and
not equal to `filtered_image.npy`; stored `texture_features` exact-match the
raw-crop GLCM (not the filtered-crop); `confidence`/`classification_label`/
`texture_features` persisted; filtered stream emitted only for
`possible_oil_spill`; rejected candidates retained; no rows deleted; later
statuses never overwritten; no `confirmed` label anywhere.

---

## 12. Open / decisions still required

1. **Validated thresholds** — `SAR_MIN_AREA_M2` and `SAR_BRIGHT_TARGET_THRESHOLD`
   need values from validation data before production use.
2. **Raw-SAR availability for older scenes** — scenes processed before Step 5
   have no `raw_image.npy`; the lookalike worker skips scoring those (retains
   the row) with a clear log.
3. **Step 6 evidence fusion** — `context_score` is the reserved input; the
   `spill.candidates.filtered` consumer group is unassigned.