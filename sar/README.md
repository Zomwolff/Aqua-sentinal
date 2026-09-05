# SAR — consolidated Synthetic Aperture Radar pipeline

All SAR-related code lives here (refactored from repo root / `services/`).

## Layout

| Path | Origin | Purpose |
|---|---|---|
| `sar-spill-intelligence/` | `services/sar-spill-intelligence/` | Lee despeckle → Otsu + CFAR → morphology → polygonize worker (`sar.clean` → `spill.candidates.raw`) |
| `acquisition/` | `services/data-ingestion/app/sar_acquisition.py`, `synthetic_injection.py`, `dynamic_sar_worker.py` | Sentinel-1 GEE acquisition, synthetic slick injection, dynamic tasking worker |
| `fetch/` | `fetch_*.py`, `gee_test.py` | Ad-hoc GEE Sentinel-1 fetch probes |
| `scripts/` | `scripts/demo_sar_spill.py`, `generate_synthetic_sar_fixture.py`, `test_demo_sar_spill.py` | E2E demo + deterministic fixture |
| `tests/` | root `test_cfar.py`, `test_pipeline.py`, `test_segmentation*.py`, `test_sar_ui.py`, `test_redis.py`, `tests/test_sar_acquisition.py`, `test_synthetic_injection.py`, `test_sar_fusion.py` | Loose + service SAR tests |
| `tools/` | `create_synthetic_spill_tiff.py`, `strip_crs.py` | Raster utilities |
| `docs/` | `docs/SAR_DEMO.md`, `docs/sar-spill-pipeline.md` | SAR run-book + pipeline spec |
| `data/sample_sar/` | `data/sample_sar/` | Sample SAR rasters (fixture default: `sar/data/sample_sar/synthetic_e2e.tif`) |

## Wiring notes

- `docker-compose.yml` builds `sar-spill-intelligence` from `sar/sar-spill-intelligence/Dockerfile`.
- `services/data-ingestion/Dockerfile` copies `sar/acquisition/*.py` into `/app/` so existing
  `from app.sar_acquisition / app.synthetic_injection / app.dynamic_sar_worker` imports keep working
  inside the container. `sar/acquisition/` modules also have a `try/except` fallback to
  `sar.acquisition.*` for local repo-root runs.
- Tests in `sar/tests/` add both repo root and `sar/acquisition/` to `sys.path` for the same reason.
- Full pipeline docs: `sar/docs/sar-spill-pipeline.md`, demo guide: `sar/docs/SAR_DEMO.md`.
