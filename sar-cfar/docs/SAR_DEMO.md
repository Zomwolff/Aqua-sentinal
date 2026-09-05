# SAR Spill Demo — End-to-End Execution Guide

This document describes how to run the complete SAR spill demonstration (Step 10) using the real Docker pipeline.

---

## Prerequisites

1. **Docker & Docker Compose** installed
2. **GEE Service Account** with Earth Engine access:
   - Service account email in `.env` as `GEE_SERVICE_ACCOUNT`
   - Private key JSON at `./secrets/gee-key.json` (git-ignored)
3. **Sentinel-1 data availability** for the Mumbai AOI in your date range

---

## Quick Start

```bash
# 1. Start infrastructure
docker compose up -d postgres redis

# 2. Wait for health (≈10-15s)
docker compose ps

# 3. Build data-ingestion (first time or after changes)
docker compose build data-ingestion

# 4. Start data-ingestion + SAR processor
docker compose up -d data-ingestion sar-spill-intelligence

# 5. Verify both services healthy
curl -sf http://localhost:8001/health | python3 -m json.tool
curl -sf http://localhost:8008/health | python3 -m json.tool

# 6. Run the demo (inside data-ingestion container)
docker compose cp sar/scripts/demo_sar_spill.py data-ingestion:/app/demo_sar_spill.py
docker compose exec data-ingestion python /app/demo_sar_spill.py --run-a --run-b
```

---

## Demo Modes

### Run A — Real Sentinel-1 Imagery
```bash
docker compose exec data-ingestion python /app/demo_sar_spill.py --run-a \
  --start 2024-01-01 --end 2024-01-31
```
- Queries GEE for real Sentinel-1 GRD scenes over Mumbai AOI
- Exports best-coverage VV-polarized scene to Drive
- Downloads GeoTIFF to `/data/artifacts/sar/`
- Publishes `sar.clean` → triggers full pipeline
- **Output:** Real candidate detections (may be 0 if no dark targets)

### Run B — Controlled Synthetic Validation
```bash
docker compose exec data-ingestion python /app/demo_sar_spill.py --run-b \
  --start 2024-01-01 --end 2024-01-31
```
- Same acquisition flow but injects synthetic slick into downloaded raster
- `is_synthetic=true` propagated through every boundary
- **Validates:** `is_synthetic` survives `sar.clean` → `spill_candidates` → `spill.candidates.filtered` → `incident.fused`

### Both Runs (default)
```bash
docker compose exec data-ingestion python /app/demo_sar_spill.py
```

---

## Expected Output

### Run A (Real)
```
========== RUN A — REAL SENTINEL-1 IMAGERY ==========
[RUN A] acquisition triggered (inject_synthetic=False)
[RUN A] waiting for SAR processing (sar.clean publish)...
[RUN A] scene_id=COPERNICUS/S1_GRD/S1A_IW_GRDH_... acquisition_time=2024-01-15T01:23:45Z is_synthetic=False
[RUN A] waiting for lookalike/scoring (spill_candidates persistence)...
[RUN A] candidate count: N
[RUN A] waiting for spill.candidates.filtered...
[RUN A] filtered events: M
[RUN A] waiting for evidence fusion (incident.fused)...
[RUN A] incident.fused events: K
[RUN A] provenance (is_synthetic==False): OK
```

### Run B (Synthetic)
```
========== RUN B — CONTROLLED SYNTHETIC VALIDATION ==========
[RUN B] acquisition triggered (inject_synthetic=True)
[RUN B] scene_id=COPERNICUS/S1_GRD/S1A_IW_GRDH_... acquisition_time=2024-01-15T01:23:45Z is_synthetic=True
[RUN B] candidate count: N (includes synthetic)
[RUN B] filtered events: M (synthetic survives filtering)
[RUN B] incident.fused events: K (synthetic reaches fusion)
[RUN B] provenance (is_synthetic==True):
  - data-ingestion/sar.clean: OK
  - PostGIS/spill_candidates: OK
  - spill.candidates.filtered: OK
  - incident.fused: OK
```

---

## Key Files & Locations

| Component | Path |
|-----------|------|
| Demo script | `sar/scripts/demo_sar_spill.py` |
| SAR acquisition | `sar/acquisition/sar_acquisition.py` |
| Synthetic injection | `sar/acquisition/synthetic_injection.py` |
| SAR worker | `sar/sar-spill-intelligence/app/worker.py` |
| Lookalike engine | `services/lookalike-engine/app/worker.py` |
| Evidence fusion | `services/evidence-fusion/app/worker.py` |
| Downloaded rasters | `/data/artifacts/sar/` (in container, `sar-scene-artifacts` volume) |
| Scene artifacts | `/data/artifacts/<scene_id>/` (processed NPY + metadata) |

---

## Environment Variables (`.env`)

```bash
# Required for SAR acquisition
GEE_SERVICE_ACCOUNT=your-sa@project.iam.gserviceaccount.com
GEE_PRIVATE_KEY_PATH=/run/secrets/gee-key.json

# Optional: enable synthetic by default
INJECT_SYNTHETIC=false

# SAR detection thresholds (must be set for pipeline to run)
SAR_MIN_AREA_M2=5000
SAR_BRIGHT_TARGET_THRESHOLD=18.0
```

---

## Troubleshooting

| Issue | Resolution |
|-------|------------|
| `GEE_SERVICE_ACCOUNT` not set | Add to `.env` and restart data-ingestion |
| `gee-key.json` not found | Place at `./secrets/gee-key.json` (mounted to `/run/secrets/gee-key.json`) |
| EE export timeout | Increase `max_wait` in `_download_exported_file()` (default 600s) |
| No scenes found | Expand date range; verify AOI has Sentinel-1 coverage |
| `SAR_MIN_AREA_M2` unset | Set in `.env` or `docker-compose.yml` — pipeline fails fast if missing |
| `rasterio` validation fails | Check downloaded file size > 0; verify Drive API permissions |

---

## Pipeline Flow Diagram

```
┌─────────────────┐
│  data-ingestion │
│  (GEE + Drive)  │
│  run_sar_acq()  │
└────────┬────────┘
         │ exports to Drive, downloads to /data/artifacts/sar/
         ▼
┌─────────────────┐     Redis Stream: sar.clean
│  sar-spill-intel│──────────────────────────────►
│  (segmentation) │                                │
└────────┬────────┘                                │
         │ writes scene artifacts                  ▼
         ▼                         ┌─────────────────────────┐
┌─────────────────┐               │      lookalike-engine   │
│  /data/artifacts/◄──────────────│  (classification + GLCM)│
│  <scene_id>/      │  NPY files  └───────────┬─────────────┘
└─────────────────┘                           │
                                              ▼
                                    Redis: spill.candidates.filtered
                                              │
                                              ▼
                                    ┌─────────────────────┐
                                    │   evidence-fusion   │
                                    │  (vessel correlation)│
                                    └──────────┬──────────┘
                                               │
                                               ▼
                                    Redis: incident.fused
                                               │
                                               ▼
                                    ┌─────────────────────┐
                                    │      api-gateway    │
                                    │  (dashboard queries)│
                                    └─────────────────────┘
```

---

## Verification Checklist

After demo completes, verify:

- [ ] `docker compose ps` shows all services healthy
- [ ] `curl localhost:8001/health` → `"worker_alive": true`
- [ ] `curl localhost:8008/health` → `"worker_alive": true`
- [ ] Run A: real scene processed, candidates persisted to `spill_candidates` table
- [ ] Run B: `is_synthetic=true` at all 4 boundaries (sar.clean, spill_candidates, filtered, fused)
- [ ] Downloaded GeoTIFF at `/data/artifacts/sar/<scene_id>.tif` opens in rasterio
- [ ] Scene artifacts at `/data/artifacts/<scene_id>/` contain 5 NPY files + metadata.json

---

## Clean Up

```bash
# Stop services
docker compose down

# Remove volumes (⚠️ deletes Postgres data + SAR artifacts)
docker compose down -v
```
