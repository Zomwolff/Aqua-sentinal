# EO correction: what changed and why

The first delta got "EO" wrong. It treated `services/data-ingestion/app/weather.py`
(wind/current fetch) as the EO service, because that was the only dormant,
environmental-sounding code in the repo at the time. That's not EO — EO
(Earth Observation) means satellite imagery measuring reflected/emitted
electromagnetic radiation. Wind/current data isn't imagery at all; it's
marine conditions data that feeds `drift-forecast`'s physics.

The `docs.zip` upload surfaced the real EO asset, in the same "written but
never wired up" state as `weather.py` was: `optical imagery/` — a Sentinel-2
10-band CNN, trained (checkpoint included), classifying MADOS classes
including "Oil Spill", never imported by anything.

## What moved

| Piece | Delta 1 (wrong) | Delta 2 (this one) |
|---|---|---|
| `eo` container | wrapped `weather.py` (wind/current ingestion) | wraps `optical imagery/` (Sentinel-2 CNN) |
| `drift-forecast` | lived in `eo` (wrong home — it's not EO, it's physics) | moved to `backend`, alongside the other decision/forecast logic |
| `weather.py` | wired up inside `eo` | **still unwired** — this delta didn't touch it; it's a separate, still-open issue, unrelated to EO |

## Why optical is wired as *secondary* evidence, not a parallel detector

Your own `docs/context-files/oil-context.md` already specifies this:

> SAR detects candidate → Check Sentinel-2 → If cloud-free → Additional evidence
> Don't make Sentinel-2 mandatory because optical imagery can be blocked by clouds.

So the `eo` service doesn't run independently looking for spills — it
subscribes to `spill.candidates.filtered` (SAR candidates that already
survived `lookalike-engine`'s oil-vs-lookalike filter), and for each one:

1. looks up the candidate's centroid + acquisition time from `spill_candidates`,
2. searches Sentinel-2 (via GEE) for a cloud-free scene near that
   location/time (`docker/eo/app/gee_optical.py`),
3. if none exists within the search window → writes `optical_cloud_free =
   false`, `optical_checked_at = now()`, and stops. **This is a normal,
   silent outcome**, not a failure — it happens whenever clouds blocked the
   pass, which is often.
4. if a scene exists → exports it, runs the CNN, writes
   `optical_oil_probability` / `optical_predicted_class` back onto the
   candidate's row, and publishes `optical.confirmed`.

`evidence-fusion` was extended (not rewritten) to carry this through:
`fuse_evidence()` gained an `optical` parameter with the same "may be null,
null is not a negative signal" treatment as the existing `environment`
parameter. Because the Sentinel-2 search + CNN run is slower than SAR-driven
fusion (it can take minutes; GEE exports aren't instant), **fusion never
waits for it** — the incident is fused and published from SAR+AIS evidence
immediately, same as before. When optical evidence resolves later, it's
attached as enrichment: `evidence-fusion` now also consumes
`optical.confirmed` and republishes a small `incident.enriched` delta
(`candidate_id` + `optical` block) that anything downstream can react to
without needing to understand the original `incident.fused` schema.

## File positions (new/changed only)

```
infra/postgres/migrations/002_optical_confirmation.sql   # NEW — 6 columns on spill_candidates
services/evidence-fusion/app/evidence.py                 # EDITED — +optical param, +optical columns in lookup SQL
services/evidence-fusion/app/worker.py                   # EDITED — +optical.confirmed consumer, +enrichment publish
docker/eo/model/sentinel2_interface.py                   # copied verbatim from `optical imagery/`
docker/eo/model/train_cnn.py                             # copied verbatim from `optical imagery/`
docker/eo/model/sentinel2_cnn_10band_best.pth             # copied verbatim from `optical imagery/`
docker/eo/app/__init__.py                                 # NEW
docker/eo/app/gee_optical.py                              # NEW — Sentinel-2 GEE search/export/download
docker/eo/app/main.py                                     # NEW — worker + /classify/upload + /health
docker/eo/Dockerfile                                       # NEW
docker/eo/requirements.txt                                 # NEW
docker/sar/Dockerfile                                       # EDITED — sar/ -> sar-cfar/ COPY paths
docker/backend/Dockerfile                                   # EDITED — +drift-forecast as a 6th process
docker/backend/requirements.txt                             # EDITED — +numpy for drift-forecast
docker/backend/supervisord.conf                              # EDITED — +drift-forecast program block
docker-compose.yml                                            # REWRITTEN — eo now the CNN, GEE creds shared
                                                                #   with sar, drift-forecast env vars moved
                                                                #   to backend (including the new oil-physics
                                                                #   params: OIL_DENSITY_KG_M3, WINDAGE_*,
                                                                #   WIND_DEFLECTION_* — drift-forecast picked
                                                                #   these up since the last delta)
```

`AIS/services/api-gateway/app/main.py` and `frontend/nginx.conf` needed the
same mechanical hostname edits as before (drift-forecast now proxies to
`backend:8012` instead of the old, wrong `eo:8012`). `eo` itself is not in
`api-gateway`'s proxy map — nothing calls it via request-time proxying, it's
event-driven off the candidate stream plus its own direct `/classify/upload`.

## Still open (not part of this delta)

- **`weather.py`** is still unwired. It's a real, separate bug (drift-forecast
  silently uses hardcoded wind/current defaults) but it isn't EO — happy to
  wire it up as its own small addition if useful, but it doesn't belong in
  the `eo` container.
- SAR now uses the UNet-ResNet34 ONNX model documented in `fusion/README.md`.
