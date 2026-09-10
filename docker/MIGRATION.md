# Migration guide — 18 containers → 8

## What changed

The old `docker-compose.yml` (kept as `docker-compose.original.yml` for
reference) ran 16 separate backend processes as 16 separate containers, plus
postgres, redis, two frontends, and a simulator. Almost none of that
separation was doing anything for you at hackathon scale — every service is
a single small FastAPI + one asyncio worker, and they don't scale
independently or get deployed independently. This delta merges them into
**4 pipeline containers** (`ais`, `sar`, `eo`, `backend`) using
[supervisord](http://supervisord.org/) to run each original, **completely
unmodified** service as its own process, still bound to its own original
port. Nothing about any service's internal logic changed — only its
hostname (its own container name → `ais`/`sar`/`eo`/`backend`).

```
18 containers                          8 containers
──────────────────────────             ──────────────────────
postgres                        →      postgres
redis                            →      redis
ais-reader                 ┐
ais-analytics               │
anomaly-detection           │
dark-vessel-detection       ├──────→   ais
ais-spoof-detection          │
sts-detection                │
vessel-risk-engine          ┘
data-ingestion (GEE/SAR)    ┐
sar-spill-intelligence       ├──────→   sar
lookalike-engine            ┘
(nothing existed here)      ┐
drift-forecast               ├──────→   backend
                             ┘
api-gateway                 ┐
evidence-fusion              │
source-attribution           ├──────→   backend
severity-impact              │
response-decision           ┘
dashboard  (Leaflet)        →   (kept in repo, unused by default)
frontend   (MapLibre)       →      frontend
simulator                   →      simulator (now behind `--profile tools`)
```

## Only 2 files were edited (both mechanical URL/hostname changes)

- `AIS/services/api-gateway/app/main.py` — the `_SERVICES` dict now points
  at `ais:PORT` / `sar:PORT` / `eo:PORT` / `backend:PORT` instead of each
  service's old standalone container name.
- `frontend/nginx.conf` — `proxy_pass` targets now point at `backend:8015`
  instead of `api-gateway:8000`.

Every other file under `AIS/`, `services/`, `sar/`, `shared/` is **byte-for-
byte the same business logic** as before. The only automated change applied
at *build time* (not to your source tree) is a one-line `sed` in each
Dockerfile that rewrites each service's hardcoded
`sys.path.insert(0, "/app")` to its new isolated path
(`/svc/<service-name>`), because several services now share one image
instead of owning `/app` outright.

## Port map (unchanged port numbers, new hostnames)

| Original container         | New host:port           |
|-----------------------------|--------------------------|
| ais-reader                  | `ais` (no port, background only) |
| ais-analytics                | `ais:8002`               |
| anomaly-detection            | `ais:8003`               |
| dark-vessel-detection        | `ais:8004`               |
| ais-spoof-detection          | `ais:8005`               |
| sts-detection                | `ais:8006`               |
| vessel-risk-engine           | `ais:8007`               |
| data-ingestion               | `sar:8001`               |
| sar-spill-intelligence       | `sar:8008`               |
| lookalike-engine             | `sar:8009`               |
| eo-ingest (**new**)          | `eo:8016`                |
 | drift-forecast               | `backend:8012`          |
| evidence-fusion              | `backend:8010`           |
| source-attribution           | `backend:8011`           |
| severity-impact              | `backend:8013`           |
| response-decision            | `backend:8014`           |
| api-gateway                  | `backend:8015` (published to host `8015`) |

## Honest ML framing (you asked me to check, not overclaim)

- **`ais` is not really an ML container.** Loitering scores, spoof
  detection, STS pairing, dark-vessel matching, and risk scoring are all
  explainable threshold/rule engines — the project's own build notes say so
  explicitly ("a simple heuristic ... is good enough, don't reach for an ML
  model here"). The one exception is `anomaly-detection`, which does
  periodically retrain a small scikit-learn model into a shared
  `ml-models` volume (`ML_TRAIN_INTERVAL_S`, `ML_MODEL_DIR`).
- **`sar`'s real model** is `lookalike-engine` — a classifier separating
  real oil slicks from biogenic slicks / low-wind look-alikes using
  engineered features (texture, persistence, wind, vessel proximity). CFAR
  + despeckle + morphology upstream of it are classical signal processing,
  not learned.
- **`eo` is new** and solves a real bug, not just a rename: the repo already
  had `services/data-ingestion/app/weather.py`, which fetches wind (Open-
  Meteo forecast API) and ocean current (Open-Meteo Marine API) data for
  free, no API key, and writes it into the existing `environmental_conditions`
  Postgres table — but nothing ever imported or started it. Because of that,
  `drift-forecast` was always silently falling back to a hardcoded default
  (0.5 m/s wind @ 225°, 0.2 m/s current @ 200°) instead of real data. The
  `eo` container now actually runs that ingestion on a 15-minute poll loop
  (`EO_POLL_INTERVAL_S`), so drift forecasts use live conditions.
  `drift-forecast` itself is a physics simulation (ITOPF 3%-wind-drift rule
  run forward and backward in time), not a trained model — see the docstring
  in `docker/eo/app/main.py` for where a learned correction model would
  plug in if you want a genuinely trained "EO model" later.

## Running it

```bash
cp .env.example .env        # fill in AISSTREAM_API_KEY / GEE creds as before
docker compose up --build   # postgres, redis, ais, sar, eo, backend, frontend
docker compose --profile tools up simulator   # optional synthetic AIS feed
```

Frontend: http://localhost:3000
Backend API (was api-gateway): http://localhost:8015

## If something doesn't come up

Each consolidated container's processes are supervisord programs; check
which one is unhealthy with:

```bash
docker compose logs -f ais       # or sar / eo / backend
docker compose exec ais supervisorctl status
```

`supervisorctl status` inside any of the four will show each original
service (e.g. `anomaly-detection`, `vessel-risk-engine`) individually, with
its own restart count — the granularity you had before is still there for
debugging, you just don't pay for it in container overhead anymore.

## Things I deliberately did NOT change

- `dashboard/` (the older Leaflet frontend) is left alone and unused by
  default — swap `frontend/build.context` in `docker-compose.yml` to
  `./dashboard` if you'd rather ship that one.
- Postgres schema, migrations, and Redis stream names/contracts are all
  untouched — this is purely a container-topology change.
- `simulator` still works exactly as before, just opt-in via
  `--profile tools` so it doesn't run in every `docker compose up`.
