# Oil-Spill-SIH — Complete Local Run Guide

> **Operational runbook** for the **Aqua Sentinel** maritime oil-spill detection and
> attribution system (the "Oil-Spill-SIH" application in this repository).
> Every command, port, stream name, environment variable, service name and endpoint in
> this document was cross-checked against the **current code** in this repository:
> `docker-compose.yml`, `Makefile`, `README.md`, the per-service `app/main.py` +
> `app/worker.py`, `shared/`, `infra/postgres/init.sql`, `scripts/`, the dashboard
> source, and the docs in `docs/`. Nothing here is guessed.
>
> If a piece of existing documentation disagrees with the code, this document reflects
> **the current working behavior of the code** (see Section 22).
>
> **Read this first:** the AIS/vessel pipeline is the fully wired, exercisable path.
> The SAR oil-spill pipeline is implemented and is testable end-to-end **only through
> the synthetic demo** (Section 14), because real Sentinel-1 acquisition requires
> Google Earth Engine (GEE) credentials and Google Drive export permissions. Read
> Section 6 and Section 23 before attempting any real SAR acquisition.

---

## 1. Overview

Aqua Sentinel is a pipeline of Python 3.11 + FastAPI microservices. Raw AIS vessel data
and SAR (Synthetic Aperture Radar) imagery are ingested, validated and pushed onto
**Redis Streams**. Worker services consume those streams, persist results to a single
**PostgreSQL 15 + PostGIS** store, and publish derived events onto the next stream. An
**API gateway** exposes a REST API and a live WebSocket relay. A React + Vite + Leaflet
**dashboard** visualizes vessels, anomalies and oil-spill candidates live on a map.

The two independently-exercisable data flows share the same PostGIS store and Redis:

1. **AIS / vessel-risk pipeline** (Section 12) — `data-ingestion` / `ais-reader` →
   `ais-analytics` → `anomaly-detection`, `ais-spoof-detection`, `sts-detection` →
   `vessel-risk-engine` → `api-gateway` → `dashboard`.
2. **SAR spill-intelligence pipeline** (Section 11) — `data-ingestion` →
   `sar-spill-intelligence` → `lookalike-engine` → `evidence-fusion` →
   `api-gateway` → `dashboard`.
3. **dark-vessel-detection** — an auxiliary SAR-driven flow consuming `sar.clean`.
4. **Skeleton services** — `source-attribution`, `drift-forecast`, `severity-impact`,
   `response-decision` are heartbeat-only shells (no worker or pipeline wiring yet).

---

## 2. Architecture

### 2.1 Communication mechanisms

| Mechanism | Where it is used | Example |
|---|---|---|
| Asynchronous **Redis Streams** | All worker inter-service hand-offs | `ais.clean` → `ais.features` → `anomaly.events`; `sar.clean` → `spill.candidates.raw` → `spill.candidates.filtered` → `incident.fused` |
| Synchronous **HTTP** | api-gateway → each service `/health`; dashboard ↔ api-gateway REST | `http://data-ingestion:8000/health`; `GET /spill/candidates/{id}` |
| **PostgreSQL / PostGIS** | every service reads/writes via `shared/db` (`asyncpg`) | `vessels`, `vessel_positions`, `spill_candidates`, `spill_incidents`, `vessel_risk_scores` |
| **WebSocket** | api-gateway `/live` (host port `8015`) → dashboard | `ws://localhost:8015/live` |
| **Shared artifact volume** | named volume `sar-scene-artifacts` mounted at `/data/artifacts` | processed SAR `.npy` bundles (Section 11) |

### 2.2 SAR spill flow

```
data-ingestion ──(sar.clean)──▶ sar-spill-intelligence
     ──(spill.candidates.raw)──▶ lookalike-engine
     ──(spill.candidates.filtered)──▶ evidence-fusion
     ──(incident.fused)──▶ api-gateway ──(WebSocket / REST)──▶ dashboard
```

Concretely: `data-ingestion → Earth Engine → SAR acquisition/download → sar.clean →
sar-spill-intelligence (Lee filter → Otsu + CFAR → morphology → polygonize → PostGIS +
artifact volume) → spill.candidates.raw → lookalike-engine (shape + GLCM texture +
scoring) → spill.candidates.filtered → evidence-fusion (spatial/temporal vessel
correlation) → incident.fused`. See Section 11.

### 2.3 AIS / vessel-risk flow

```
ais-reader (AISStream WebSocket / VesselAPI poll)   or   data-ingestion POST /ingest/ais   or   simulator replay
   │  ais.clean
   ▼
ais-analytics ──(ais.features)──▶ anomaly-detection ──(anomaly.events)──┐
   │                                    │                                │
ais-spoof-detection ──(ais.trust)────────┴─▶ vessel-risk-engine ─(vessel.risk)──▶ api-gateway
                           sts-detection ──(sts.events)───────────▲
```

`api-gateway` relays `anomaly.events`, `vessel.risk`, `sts.events`,
`spill.candidates.filtered` and `incident.fused` to connected WebSocket clients.
Independent flow: `dark-vessel-detection` consumes `sar.clean` → emits
`dark.vessel.events`.

---

## 3. Prerequisites

These are the tools the repository actually references (from `docker-compose.yml`,
`Dockerfile`s, `README.md`, `Makefile`):

| Tool | Required? | Version requirement |
|---|---|---|
| **Docker** (daemon) | Required to run the stack | **Version not pinned by the repository.** |
| **Docker Compose v2** (`docker compose`, the plugin form) | Required | **Version not pinned.** `README.md` also mentions the standalone `docker-compose` as an option, but all commands here use `docker compose`. |
| **Make** | Optional — only if you want the `Makefile` shortcuts (`make up`, `make logs`, …) | Version not pinned. All commands below are shown as raw `docker compose …` so Make is unnecessary. |
| **Python 3.11** | Only for running scripts/tests natively on the host (e.g. `scripts/seed_demo_data.py`, `scripts/verify_db.py`, `run_ais_reader.py`, the test suites). The containers use the `python:3.11-slim` image for **every** service. | `3.11` (all `Dockerfile` are `FROM python:3.11-slim`). |
| **Node.js / npm** | Only for the dashboard's **local dev server** (`dashboard/`). The dashboard **Docker** build uses the official `node:20-alpine` image. | Host Node version not pinned; the Docker build is `node:20`. |
| **Git** | Required to clone the repository | Version not pinned. |
| **Google Earth Engine account + Google Cloud project** | Required **only** for **real** SAR acquisition (Section 6 and Section 11). Not required for the synthetic demo (Section 14) or the AIS pipeline. | Not pinned. |
| **AISStream.io API key** (`AISSTREAM_API_KEY`) | Optional — needed to ingest **live** AIS; without it the reader logs an error and stays idle. Not needed for `simulator` replay or `POST /ingest/ais`. | Not pinned. |
| **VesselAPI API key** (`VESSELAPI_API_KEY`) | Optional — needed for the VesselAPI HTTP polling provider in `ais-reader`. | Not pinned. |

> Any tool not listed (e.g. a specific PostGIS/Redis version on the host) is **not** a
> prerequisite: PostgreSQL, PostGIS and Redis all run **inside** Docker containers
> (`postgis/postgis:15-3.4` and `redis:7-alpine`).

---

## 4. Repository Structure

```
.
├── docker-compose.yml         # Full-stack orchestration (16 backend-ish services + infra + dashboard + simulator)
├── Makefile                   # Convenience: up, up-ais, logs, health, db-*, redis-* , clean, reader
├── README.md, task.md, ais-demo.md, ais-ml.md, ais-services.md
├── .env.example               # Environment template (copy to .env)
├── .env                       # git-ignored, your real values
├── .gitignore / .dockerignore
├── docs/                      # api-contracts.md, spatial.md, SAR_DEMO.md, sar-spill-pipeline.md, architecture.md, context-files/
├── infra/
│   ├── postgres/init.sql      # PostGIS schema (run once on first volume creation)
│   ├── postgres/migrate_stage1..5.sql   # idempotent migration files (Section 7.4)
│   └── redis/redis.conf
├── shared/                    # canonical shared Python package
│   ├── spatial/               #   constants.py, geo.py (geography-cast helpers)
│   └── db/                    #   connection.py, models.py
├── services/shared/           # AIS-pipeline shared modules (redis_client.py, db.py, geo_utils.py, models.py, artifacts.py)
├── services/<service>/        # per-service: Dockerfile, requirements.txt, app/{main.py,worker.py,...}
├── dashboard/                 # ACTIVE frontend (React + Vite + Leaflet), Docker + nginx, port 3000
├── frontend/                  # STALE / INACTIVE first-draft frontend (maplibre; NOT in docker-compose)
├── simulator/                 # data replay container (prints, mounts ./data, no HTTP)
├── scripts/                   # demo_sar_spill.py, seed_demo_data.py, verify_db.py, generate_synthetic_sar_fixture.py, test_demo_sar_spill.py
├── tests/                     # pytest regression suites (spatial)
├── data/                      # sample_sar/, sample_ais/, env_layers/, geo_layers/ (mounted read-only into simulator)
├── run_ais_reader.py          # host-side native AIS reader (make reader)
└── secrets/                   # git-ignored; holds gee-key.json
```

### 4.1 Directory conventions

- Each `services/<service>/` is a Python 3.11 microservice with its own `Dockerfile`,
  `requirements.txt`, and an `app/` package (FastAPI + `main.py`, and a `worker.py`
  for the pipeline workers).
- `docker-compose.yml` build context is always the repo root (`.`) and the Dockerfile is
  `<service>/Dockerfile`.
- There are **two** shared packages and the project distinguishes them deliberately
  (this is a real source of confusion — see Section 20 "shared/".):
  - `shared/` — the canonical top-level package (its `spatial/` + `db/`). Used by the
    SAR services (`sar-spill-intelligence`, `lookalike-engine`, `evidence-fusion`) and
    data-ingestion (`shared/db` is used as a **module** `services/shared/db.py` here).
  - `services/shared/` — the AIS-pipeline shared modules (`redis_client.py`, `db.py`,
    `geo_utils.py`, `models.py`, `artifacts.py`). Note the `db.py` here is a **module**
    while top-level `shared/db/` is a **package** — they cannot both be mounted/copied
    into the same `/app/shared` for the SAR services, so each Docker/image and
    compose volume mounts selects which one it needs (see Section 20).

### 4.2 Which "frontend" is active

- **Active:** `dashboard/` — referenced in `docker-compose.yml` (service `dashboard`,
  host port `3000`), React + Vite + **Leaflet**, served by nginx in Docker, dev server
  on `:3000`. This is the one to use.
- **Stale/inactive:** `frontend/` — a first-draft maplibre dashboard. It is **not**
  referenced by `docker-compose.yml`, has no tests, and is not built by any make target.
  Ignore it (details in Section 15).

---

## 5. Environment Configuration

### 5.1 Base clone / first start

All commands in this runbook are executed from the repository root. There is no
host-side dependency installation needed for the Docker path — images pull all Python
and Node dependencies.

```bash
# Clone (the repository's origin remote as configured in git):
git clone git@github.com:Zomwolff/Aqua-sentinal.git
# or, if you only have the public HTTPS remote:
# git clone https://github.com/Zomwolff/Aqua-sentinal.git
cd Aqua-sentinal

# Create your .env from the template:
cp .env.example .env
```

> The checked-out directory name matters for Docker Compose: the **project name** is the
> lower-cased directory basename, and Compose prefixes containers, volumes and the
> network with it. With folder `Aqua-sentinal` the bridge network is
> `aqua-sentinal_aqua-net` (this is exactly what `README.md` and the test command use).
> If the folder is named differently the network becomes `<folder>_aqua-net`.

### 5.2 Environment variables

`.env` is read by `docker compose` automatically and injected into every container (the
`backend-env` anchor in `docker-compose.yml` plus per-service overrides). The table lists
every variable **actually referenced** by the code/compose file. Secrets are placeholders.

Legend: REQUIRED (fails fast / cannot do its job without it) · OPTIONAL (code default) ·
DEMO/TEST ONLY (only used by scripts/demo).

| Variable | Service | Class | Default (.env.example) | Purpose | Secret? |
|---|---|---|---|---|---|
| `POSTGRES_USER` | postgres + all | REQUIRED | `postgres` | DB user created on first volume init | No |
| `POSTGRES_PASSWORD` | postgres + all | REQUIRED | `postgres` | DB password | **Yes** |
| `POSTGRES_DB` | postgres + all | REQUIRED | `maritime_oilspill` | Database name | No |
| `POSTGRES_HOST` | all | REQUIRED | `postgres` | DB hostname (compose service) | No |
| `POSTGRES_PORT` | all | OPTIONAL | `5432` | Internal DB port (container 5432) | No |
| `REDIS_HOST` | all | REQUIRED | `redis` | Redis hostname | No |
| `REDIS_PORT` | all | OPTIONAL | `6379` | Redis internal port (container 6379) | No |
| `PORT` | every FastAPI svc | OPTIONAL | `8000` | Container-internal uvicorn port | No |
| `AIS_PROVIDER` | data-ingestion, ais-reader | OPTIONAL | `aisstream` | `none` / `aisstream` / `vesselapi` | No |
| `AISSTREAM_API_KEY` | data-ingestion, ais-reader | OPTIONAL | (empty) | AISStream.io key | **Yes** |
| `VESSELAPI_API_KEY` | ais-reader | OPTIONAL | (empty) | VesselAPI bearer token | **Yes** |
| `VESSELAPI_BBOX_MIN_LAT/LON/MAX_LAT/LON` | ais-reader | OPTIONAL | `18.4/71.5/19.8/73.0` | VesselAPI Mumbai bbox (≤4° span) | No |
| `VESSELAPI_POLL_INTERVAL_SECONDS` | ais-reader | OPTIONAL | `60` | VesselAPI poll cadence | No |
| `AIS_BBOX_MIN_LAT/LON/MAX_LAT/LON` | ais-reader, data-ingestion | OPTIONAL | `14.0/68.0/25.0/77.5` | Mumbai offshore AIS bbox | No |
| `WINDOW_MINUTES` | ais-analytics | OPTIONAL | `15` | Feature window length | No |
| `PROXIMITY_RADIUS_M` | ais-analytics | OPTIONAL | `1000` | Cross-vessel proximity radius (m) | No |
| `AIS_GAP_THRESHOLD_MINUTES` | anomaly-detection | OPTIONAL | `30` | Gap anomaly threshold | No |
| `ERRATIC_COURSE_VARIANCE` | anomaly-detection | OPTIONAL | `2500` | Course variance threshold | No |
| `LOITERING_THRESHOLD` | anomaly-detection | OPTIONAL | `0.8` | Loitering threshold | No |
| `STS_DISTANCE_THRESHOLD_M` / `STS_SPEED_THRESHOLD_KN` / `STS_MIN_DURATION_MIN` | sts-detection | OPTIONAL | `500` / `2.0` / `30` | STS thresholds | No |
| `MAX_PLAUSIBLE_AIS_ERROR_M` / `MAX_PHYSICAL_SPEED_MS` | ais-spoof-detection | OPTIONAL | `500` / `25.7` | Spoof plausibility limits | No |
| `RISK_DECAY_RATE` / `RISK_TIER_MEDIUM/HIGH/CRITICAL` | vessel-risk-engine | OPTIONAL | `0.05` / `20/50/75` | Risk tuning | No |
| `DARK_VESSEL_GAP_MINUTES` / `SCAN_INTERVAL_S` / `MAX_MATCH_DISTANCE_M` / `MAX_MATCH_TIME_S` | dark-vessel-detection | OPTIONAL | `90`/`300`/`5000`/`1800` | Only compose defaults (not in .env.example) | No |
| `ML_MODEL_DIR` / `ML_TRAIN_INTERVAL_S` / `ML_MAX_MODEL_AGE_H` / `ML_MIN_SAMPLES` | anomaly-detection | OPTIONAL | `/data/models`/`3600`/`25`/`100` | ML persistence/training | No |
| `SAR_MIN_AREA_M2` | sar-spill-intelligence | **REQUIRED for SAR step 3** | (empty) | Min spill area; **NOT VALIDATED** | No |
| `SAR_BRIGHT_TARGET_THRESHOLD` | sar-spill-intelligence | **REQUIRED for SAR step 3** | (empty) | Bright-target mask threshold; **NOT VALIDATED** | No |
| `SAR_ARTIFACT_ROOT` | sar/lookalike | OPTIONAL | `/data/artifacts` | Artifact volume root | No |
| `EVIDENCE_SPATIAL_WINDOW_M` | evidence-fusion | OPTIONAL | `5000` | Correlation spatial window (m), tunable | No |
| `EVIDENCE_TEMPORAL_WINDOW_HOURS` | evidence-fusion | OPTIONAL | `6` | Correlation temporal window (h), tunable | No |
| `INJECT_SYNTHETIC` | data-ingestion | DEMO/TEST ONLY (default `false`) | `false` | Opt-in synthetic slick injection | No |
| `GEE_SERVICE_ACCOUNT` | data-ingestion (SAR) | **REQUIRED for real SAR** | (empty) | GEE service-account email | No |
| `GEE_PRIVATE_KEY_PATH` | data-ingestion (SAR) | **REQUIRED for real SAR** | `/run/secrets/gee-key.json` | Container path to mounted JSON key | No (path) |
| `PLAYBACK_SPEED` | simulator | OPTIONAL | `60` | Replay speed | No |
| `DATA_INGESTION_URL` | ais-analytics | OPTIONAL | `http://data-ingestion:8000` | HTTP base for reference layers | No |
| `VITE_WS_URL` | dashboard (dev) | OPTIONAL | (derived) | Overrides WebSocket URL in dev | No |

### 5.3 Host scripts and `POSTGRES_*`

The scripts `scripts/*.py` import `shared/db/connection.py`, whose **code defaults** are
`aqua_sentinel`/`change_me` (a known inconsistency — see Section 20). When running a
script **natively on the host**, export the DB variables so they match your running
Postgres:

```bash
export POSTGRES_USER=postgres POSTGRES_PASSWORD=postgres POSTGRES_DB=maritime_oilspill \
       POSTGRES_HOST=localhost POSTGRES_PORT=5433
```

---

## 6. Google Earth Engine Setup

Required **only** for the **real** SAR acquisition path (Run A of `scripts/demo_sar_spill.py`
and the `data-ingestion` SAR trigger). The synthetic demo (Run B) and the whole AIS
pipeline do **not** need GEE. The `docker-compose.yml` `data-ingestion` environment wires
two variables:

```yaml
GEE_SERVICE_ACCOUNT: ${GEE_SERVICE_ACCOUNT:-}
GEE_PRIVATE_KEY_PATH: ${GEE_PRIVATE_KEY_PATH:-/run/secrets/gee-key.json}
```

and the service mounts one secret file read-only:

```yaml
- ./secrets/gee-key.json:/run/secrets/gee-key.json:ro
```

So the **host path** is `./secrets/gee-key.json` (relative to the repo root) and the
**container path** is `/run/secrets/gee-key.json`. All values are placeholders; never
paste a real key.

1. **Create/select a Google Cloud project.** The service account in this repo was created
   under a Google Cloud project (the `.env` currently references the email
   `oil-spill-sar-sih@learn-langchain-3bf19.iam.gserviceaccount.com` — do **not** use
   that exact project; create your own).
2. **Enable the required APIs.** At minimum enable **Earth Engine API**
   (`earthengine.googleapis.com`). Acquisition also exports scenes to **Google Drive**,
   so enable the **Drive API** for downloads (`_download_exported_file()` uses Drive to
   fetch the exported GeoTIFF).
3. **Create a service account.** In IAM & Admin → Service Accounts, create one (e.g.
   `oil-spill-sar@<your-project>.iam.gserviceaccount.com`).
4. **Obtain the JSON key.** In the service-account row → Keys → Add Key → JSON; download
   it. This JSON is the GEE private key.
5. **Grant GEE access.** Earth Engine is now per-project: the service-account email must
   be **registered** in the Earth Engine code-editor/console
   (sign in via Earth Engine with that email, or ask the project owner to add it as a
   registered user / grant `roles/earthengine.viewer`+`writer`). Without GEE registration
   the pipeline logs an auth error at acquisition time.
6. **Put the key where the repo expects it.**
   ```bash
   mkdir -p secrets
   cp /path/to/downloaded-key.json secrets/gee-key.json
   ```
7. **Configure env** in `.env`:
   ```bash
   GEE_SERVICE_ACCOUNT=your-sa@<your-project>.iam.gserviceaccount.com
   GEE_PRIVATE_KEY_PATH=/run/secrets/gee-key.json
   ```
   `GEE_PRIVATE_KEY_PATH` is the **container** path (`/run/secrets/gee-key.json`); keep
   it as shown. The **host** location is fixed by the compose volume mount
   (`./secrets/gee-key.json`), not by this variable.

**Credentials safety (see also Section 22).** The private key JSON must **never** be
committed to Git. `.gitignore` contains `secrets/` and `.env` — both are ignored. The
compose mount is read-only (`:ro`) so the container never writes the key. Do **not** put
the key contents in any log, ticket, screenshots, or this file.

---

## 7. Database / PostGIS

- **Image:** `postgis/postgis:15-3.4` (PostgreSQL 15 + PostGIS), forced `platform:
  linux/amd64` (needed on Apple Silicon).
- **Compose service:** `postgres`. **Container names** default to
  `<project>_postgres_1` (no explicit `container_name` is set).
- **Database / user / password:** from `POSTGRES_USER`, `POSTGRES_PASSWORD`,
  `POSTGRES_DB` (defaults `postgres`/`postgres`/`maritime_oilspill` from `.env.example`).
- **Ports:** host **`5433`** ⇄ container **`5432`**.
- **Volume:** named volume `pgdata` at `/var/lib/postgresql/data`, plus
  `./infra/postgres/init.sql` mounted into the Postgres **init dir**.
- **Healthcheck:** `pg_isready -U <user> -d <db>`; app services `depends_on: postgres`
  with `condition: service_healthy`.

### 7.1 The two schema mechanisms (read this)

- **Init script (Docker entrypoint):** `infra/postgres/init.sql` is mounted at
  `/docker-entrypoint-initdb.d/init.sql`. The official Postgres image runs everything in
  that directory **only on first creation of a fresh `pgdata` volume**. It creates the
  PostGIS + uuid-ossp extensions, all enums, and the full schema (§7.3). Re-running it on
  an existing volume does nothing.
- **Migration files:** `infra/postgres/migrate_stage1.sql` … `migrate_stage5.sql` are
  **SQL files you apply manually** (Section 7.2). They are **idempotent** (`IF NOT
  EXISTS`, `ADD VALUE IF NOT EXISTS`) and kept aligned with `init.sql` because init.sql
  only runs once. Stages 2–4 build the `spill_candidates` table + status enum; stage 5
  adds the `is_synthetic` provenance column.

### 7.2 Start / inspect / migrate commands

```bash
# Start only the database
docker compose up -d postgres

# Wait for healthy
docker compose ps postgres

# Open a psql shell (compose exec, using the .env credentials)
docker compose exec postgres psql -U postgres -d maritime_oilspill

# Verify PostGIS
docker compose exec postgres psql -U postgres -d maritime_oilspill -c "SELECT PostGIS_Version();"

# Apply the SAR migrations (idempotent; run from the repo root)
cat infra/postgres/migrate_stage2.sql | docker compose exec -T postgres psql -U postgres -d maritime_oilspill
cat infra/postgres/migrate_stage3.sql | docker compose exec -T postgres psql -U postgres -d maritime_oilspill
cat infra/postgres/migrate_stage4.sql | docker compose exec -T postgres psql -U postgres -d maritime_oilspill
cat infra/postgres/migrate_stage5.sql | docker compose exec -T postgres psql -U postgres -d maritime_oilspill

# Verify the full standard schema programmatically
pip3 install -r scripts/requirements.txt
python3 scripts/verify_db.py
```

The `Makefile` also provides `make db-shell`, `make db-vessels`, `make db-anomalies`,
`make db-risk`, `make db-sts`.

### 7.3 Important tables and extensions

Extensions (from `init.sql`): `postgis`, `"uuid-ossp"`.

| Table | Purpose | Spatial |
|---|---|---|
| `vessels` | Static vessel registry. `mmsi` VARCHAR(20) UNIQUE = AIS join key; `id` BIGSERIAL internal PK | — |
| `vessel_positions` | AIS positions; `latitude`/`longitude` columns; `geom` auto-populated by DB trigger | `GEOMETRY(Point,4326)` |
| `spill_incidents` | One row per detected spill (legacy SIH table; not written by the SAR pipeline) | `geom`, `centroid` |
| `spill_candidates` | **SAR candidates** produced by `sar-spill-intelligence`; status/classification enum; `is_synthetic` | `geom GEOMETRY(Polygon,4326)` |
| `attribution_results`, `forecasts`, `severity`, `response_recommendations` | Legacy SIH output tables (skeleton services; filled by `seed_demo_data.py`) | `forecasts.geom` |
| `protected_areas`, `environmental_conditions` | Context layers | `geom` |
| `vessel_features` | 15-min feature windows from ais-analytics | — |
| `anomaly_events` | Anomaly alerts | lat/lon columns |
| `ais_trust_scores` | Rolling trust/spoof scores | — |
| `sts_events` | STS encounters | `location` |
| `vessel_risk_scores` | Per-vessel risk, `tier` enum | — |
| `dark_vessel_events` | SAR-detected dark vessels | `geom` |
| `satellite_tasking_requests` | Risk-engine tasking output | — |
| `ingest_stats`, `vessel_running_stats`, `vessel_draught_observations`, `reference_layers` | Aux/state tables | `reference_layers.geom` |

**Spatial rule (see `docs/spatial.md`):** all spatial columns are stored as
`GEOMETRY(...,4326)` (degrees, `POINT(longitude latitude)`). For real-world metres use the
geography cast — `ST_Distance(a.geom::geography, b.geom::geography)` or
`ST_DWithin(a.geom::geography, b.geom::geography, radius_meters)`. Do **not** change
`geometry` columns to `geography`.

### 7.4 No destructive reset by default

There is **no** repository-provided "reset the database" target except deleting the
volume. Do **not** run `docker compose down -v` as routine cleanup (Section 21).
`scripts/seed_demo_data.py` **does** `TRUNCATE … RESTART IDENTITY CASCADE` on the legacy
tables it seeds — that is an explicit data-seeding script; run it only when you intend to
replace demo data.

---

## 8. Redis

- **Image:** `redis:7-alpine`.
- **Compose service:** `redis`. Host port **`6380`** ⇄ container **`6379`**.
- **Config:** `./infra/redis/redis.conf` mounted read-only (`port 6379`, `appendonly
  yes`, `save 60 10000`, `protected-mode no`).
- **Healthcheck:** `redis-cli ping` → `PONG`.
- **Connection env:** `REDIS_HOST=redis`, `REDIS_PORT=6379` (service/host ports inside
  the `aqua-net` bridge network). From the host:
  `docker compose exec redis redis-cli` (or `redis-cli -p 6380` if you have a client).

### 8.1 Stream architecture

Workers use `shared/redis_client.py`. Each consumer creates a **consumer group** on the
stream it reads (`ensure_consumer_group`) and reads with `consume_stream(...)` which
auto-acknowledges messages (processing is effectively checkpointed per delivery).
`api-gateway` reads several streams with plain `xread` (no group) just to relay them to
WebSocket clients.

> Note: the api-gateway `/system/pipeline` endpoint hard-codes a list of **six AIS
> streams** (`ais.clean`, `ais.features`, `anomaly.events`, `ais.trust`, `sts.events`,
> `vessel.risk`). It does not enumerate the SAR streams, but those streams are real and
> flowing (see the table).

### 8.2 Consolidated stream table (verified against code)

| Stream | Producer | Consumer group(s) / reader | Purpose |
|---|---|---|---|
| `ais.clean` | `data-ingestion` (POST /ingest/ais) or `ais-reader` | `analytics` (ais-analytics), `spoof-detection` (ais-spoof-detection) | Validated/normalised AIS records |
| `ais.features` | `ais-analytics` | `anomaly-detection`, `sts-detection` | Per-vessel feature windows |
| `anomaly.events` | `anomaly-detection` | `risk-engine` (vessel-risk-engine); api-gateway xread→WS | Behavioral anomalies |
| `ais.trust` | `ais-spoof-detection` | `risk-engine` (vessel-risk-engine) | Rolling trust/spoof scores |
| `sts.events` | `sts-detection` | `risk-engine` (vessel-risk-engine); api-gateway xread→WS | STS encounters |
| `vessel.risk` | `vessel-risk-engine` | api-gateway xread→WS; evidence-fusion drains (group `evidence-fusion`) | Vessel risk scores |
| `sar.clean` | `data-ingestion` (SAR acquisition) | `sar-spill-intelligence`, `dark-vessel-detection` | Processed SAR scene (GeoTIFF path + metadata) |
| `spill.candidates.raw` | `sar-spill-intelligence` | `lookalike-engine` | Candidate IDs per scene |
| `spill.candidates.filtered` | `lookalike-engine` | `evidence-fusion`; api-gateway xread→WS | `possible_oil_spill` candidates |
| `incident.fused` | `evidence-fusion` | api-gateway xread→WS | Fused incident evidence (correlated vessel) |
| `dark.vessel.events` | `dark-vessel-detection` | (no assigned group yet) | Dark-vessel detections |

### 8.3 Inspecting streams

```bash
docker compose exec redis redis-cli PING
docker compose exec redis redis-cli XLEN ais.clean
docker compose exec redis redis-cli XLEN spill.candidates.raw
docker compose exec redis redis-cli XLEN incident.fused
docker compose exec redis redis-cli XINFO STREAM ais.clean
```

The `Makefile` `make redis-streams` target prints `XLEN` for the six AIS streams above.

---

(`seed_demo_data.py` populates their legacy tables directly).

---

## 9. Service Inventory

All FastAPI services run uvicorn on the container-internal port **8000** (`PORT` env) and
are exposed on a unique host port. `ais-reader` and `simulator` are **not** HTTP servers.
"Container name" is the Compose default `<project>_<service>_1` (no `container_name` is
set anywhere in `docker-compose.yml`). All FastAPI services expose `GET /health` and most
also expose `GET /status`.

| Service | Purpose | Dir | Host port | Health | Redis consumes (group) | Redis produces | DB deps | Ext. API |
|---|---|---|---|---|---|---|---|---|
| `postgres` | PostGIS 15 storage | — | 5433 | `pg_isready` healthcheck | — | — | PostGIS | — |
| `redis` | Redis 7 streams/state | — | 6380 | `redis-cli ping` | — | all streams routed here | — | — |
| `data-ingestion` | Live AIS validation/dedup/normalize + SAR acquisition trigger | `services/data-ingestion` | 8001 | `/health` | — | `ais.clean` (via POST), `sar.clean` | vessels, vessel_positions, reference_layers | AISStream(WS)/GEE+Drive (SAR) |
| `ais-reader` | Standalone AIS reader (pure Python, no HTTP) | `services/ais-reader` | — | none (no HTTP) | — | `ais.clean` | vessels, vessel_positions, vessel_draught_observations | AISStream WS / VesselAPI HTTP |
| `ais-analytics` | 15-min per-vessel feature windows | `services/ais-analytics` | 8002 | `/health` | `ais.clean` (analytics) | `ais.features` | vessels, vessel_features | data-ingestion(reference layers) |
| `anomaly-detection` | Rules + stats + ML anomaly flags | `services/anomaly-detection` | 8003 | `/health` | `ais.features` (anomaly-detection) | `anomaly.events` | vessels, vessel_features, anomaly_events, environmental_conditions | — |
| `dark-vessel-detection` | SAR-detected vessels with no AIS | `services/dark-vessel-detection` | 8004 | `/health` | `sar.clean` (dark-vessel-detection) | `dark.vessel.events` | vessel_positions, dark_vessel_events | — |
| `ais-spoof-detection` | Dead-reckoning trust/spoof scores | `services/ais-spoof-detection` | 8005 | `/health` | `ais.clean` (spoof-detection) | `ais.trust` | vessels, vessel_positions, ais_trust_scores | — |
| `sts-detection` | Ship-to-ship transfer encounter detection | `services/sts-detection` | 8006 | `/health` | `ais.features` (sts-detection) | `sts.events` | vessels, vessel_positions, sts_events | — |
| `vessel-risk-engine` | Per-vessel risk scoring | `services/vessel-risk-engine` | 8007 | `/health` | `anomaly.events`+`ais.trust`+`sts.events` (risk-engine) | `vessel.risk` | vessels, vessel_risk_scores, satellite_tasking_requests | — |
| `sar-spill-intelligence` | SAR detection (Lee/Otsu/CFAR/morphology/polygonize) | `services/sar-spill-intelligence` | 8008 | `/health`,`/status` | `sar.clean` (sar-spill-intelligence) | `spill.candidates.raw` | spill_candidates, artifact vol | rasterio(GDAL) |
| `lookalike-engine` | Shape + GLCM texture + confidence scoring | `services/lookalike-engine` | 8009 | `/health`,`/status` | `spill.candidates.raw` (lookalike-engine) | `spill.candidates.filtered` | spill_candidates, artifact vol | — |
| `evidence-fusion` | SAR + high-risk vessel correlation | `services/evidence-fusion` | 8010 | `/health`,`/status` | `vessel.risk`+`spill.candidates.filtered` (evidence-fusion) | `incident.fused` | spill_candidates, vessel_risk_scores, vessel_positions | — |
| `source-attribution` | (skeleton, heartbeat only) | `services/source-attribution` | 8011 | `/health` | — | — | (none wired) | — |
| `drift-forecast` | (skeleton, heartbeat only) | `services/drift-forecast` | 8012 | `/health` | — | — | (none wired) | — |
| `severity-impact` | (skeleton, heartbeat only) | `services/severity-impact` | 8013 | `/health` | — | — | (none wired) | — |
| `response-decision` | (skeleton, heartbeat only) | `services/response-decision` | 8014 | `/health` | — | — | (none wired) | — |
| `api-gateway` | REST API + WebSocket relay | `services/api-gateway` | 8015 | `/health`, `/system/health` | reads 5 streams (xread) for WS | — (relays only) | many tables (read-only aggregation) | internal services `/health` |
| `dashboard` | React + Vite + Leaflet UI | `dashboard/` | 3000 | (nginx) | ws `/live` via gateway | — | — | api-gateway REST/WS |
| `simulator` | data replay container (no HTTP) | `simulator/` | — | — | — | (none currently) | — | mounts `./data` |

> **Exact health endpoints per service:** every FastAPI service answers `GET /health`
> with `{status, service, worker_alive, ...counters}`. The SAR triad
> (`sar-spill-intelligence`, `lookalike-engine`, `evidence-fusion`) also expose
> `GET /status`. `api-gateway` exposes `/health`, `/system/health`, `/system/pipeline`,
> `/system/stats`, plus dozens of data endpoints (see Section 10, api-gateway).

---

## 10. Running Every Service

### 10.0 Common run commands

Every FastAPI service is built and run the same way:

```bash
# Start (build if image absent) one service
docker compose up -d <service>

# If you changed its code, rebuild:
docker compose build <service>

# Logs (follow):
docker compose logs -f <service>

# Health (every FastAPI service):
curl -sf http://localhost:<host-port>/health | python3 -m json.tool
```

The most reliable single health check for the whole stack is via the api-gateway:

```bash
docker compose up -d api-gateway
curl -sf http://localhost:8015/system/health | python3 -m json.tool
```

There is **no supported "run a microservice on the host outside Docker" path** in the
repo: the Dockerfiles assume the merged `/app/shared` layout inside a container. The only
things documented to run natively are `scripts/*.py`, the test suites (Section 19), and
`run_ais_reader.py` (`make reader`).

### 10.1 postgres

- Purpose: central PostGIS store (§7).
- Start: `docker compose up -d postgres`
- Health: `docker compose ps postgres` → status `healthy` (pg_isready).
- Logs: `docker compose logs -f postgres`
- Inspect: `docker compose exec postgres psql -U postgres -d maritime_oilspill`
- Config: `POSTGRES_USER/PASSWORD/DB`; host 5433, container 5432; volume `pgdata`.
- Expected startup: standard Postgres boot; on a **fresh** volume `init.sql` runs (you
  see `CREATE EXTENSION`/`CREATE TABLE`); on an existing volume it starts quietly.

### 10.2 redis

- Purpose: streams + shared state.
- Start: `docker compose up -d redis`
- Health: `docker compose exec redis redis-cli ping` → `PONG`; `docker compose ps redis`
  → healthy.
- Logs: `docker compose logs -f redis`
- Config: host 6380, container 6379, `./infra/redis/redis.conf`.

### 10.3 data-ingestion

- Purpose: validates/dedups/normalises AIS into Postgres + `ais.clean`; owns GEE creds +
  the SAR-acquisition trigger that publishes `sar.clean`.
- Directory: `services/data-ingestion`. Port 8001.
- Start: `docker compose up -d data-ingestion`
- Health: `curl -sf http://localhost:8001/health | python3 -m json.tool` → expect
  `"worker_alive": true`.
- Status: `curl -sf http://localhost:8001/ingest/status`
- Logs: `docker compose logs -f data-ingestion`
- Config: `AIS_PROVIDER` (compose hard-sets this service to `none` → HTTP-only), DB/Redis
  env, `INJECT_SYNTHETIC`, and for SAR: `GEE_SERVICE_ACCOUNT` + `GEE_PRIVATE_KEY_PATH`.
- Expected startup: `Redis connected …`, `PostgreSQL pool created …`, reference layers
  loaded, `data-ingestion startup complete`, then (because `AIS_PROVIDER=none`) logs
  `Accepting only POST /ingest/ais` and heartbeat every 10 s.
- Inputs: `POST /ingest/ais` (single/batch), SAR acquisition trigger.
- Outputs: `ais.clean` (HTTP path), `sar.clean` (SAR path), Postgres writes.
- Manual AIS injection (no external API needed to push data):
  ```bash
  curl -X POST http://localhost:8001/ingest/ais -H 'Content-Type: application/json' -d '{
    "mmsi":123456789,"lat":18.5,"lon":72.9,"speed_knots":10,"course":120,"heading":125,
    "nav_status":0,"timestamp":"2024-01-01T00:00:00Z","vessel_name":"Test","vessel_type_str":"cargo"
  }'
  ```
- Troubleshooting: worker alive but `messages_processed` frozen → this is **expected** in
  `AIS_PROVIDER=none` mode (only HTTP injection produces records).

### 10.4 ais-reader

- Purpose: standalone WebSocket-to-AISStream reader (or VesselAPI poll); **no HTTP
  server**.
- Directory: `services/ais-reader`. Public port: none.
- Start: `docker compose up -d ais-reader` (runs `python -u app/reader.py`).
- Health: none. Verify via logs / `docker compose ps ais-reader` (running) and that
  `ais.clean` grows: `docker compose exec redis redis-cli XLEN ais.clean`.
- Logs: `docker compose logs -f ais-reader`
- Config: `AIS_PROVIDER`, `AISSTREAM_API_KEY`, `VESSELAPI_API_KEY` + `VESSELAPI_*` bbox,
  `AIS_BBOX_*`.
- Expected startup: `AISStream subscription confirmed by first data message` with a valid
  key; without a key it logs an error and idles.
- Native option (bypasses Docker — `Makefile`): `make reader-setup` then `make reader`
  (runs `run_ais_reader.py` on the host, talking to `localhost:5433`/`localhost:6380`).
- Outputs: `ais.clean`; Postgres `vessels`, `vessel_positions`,
  `vessel_draught_observations`.
- Troubleshooting: `AISStream rejected subscription` ⇒ invalid/rate-limited key (the
  code surfaces the provider error rather than a silent disconnect).

### 10.5 ais-analytics

- Purpose: buffers `ais.clean` into per-vessel 15-min windows, computes features.
- Dir `services/ais-analytics`. Port 8002.
- Start: `docker compose up -d ais-analytics`
- Health: `curl -sf http://localhost:8002/health` (expect `worker_alive: true`).
- Endpoints: `GET /vessels`, `GET /vessels/{mmsi}/features`,
  `GET /vessels/{mmsi}/proximity`, `POST /features/recompute`.
- Consumes: `ais.clean` (group `analytics`). Produces: `ais.features`. DB: `vessels`,
  `vessel_features`.
- Config: `WINDOW_MINUTES` (15), `PROXIMITY_RADIUS_M` (1000), `DATA_INGESTION_URL`.
- Logs: `docker compose logs -f ais-analytics`
- Troubleshooting: empty windows when idle is expected (needs live `ais.clean`).

### 10.6 anomaly-detection

- Purpose: rules + statistical z-score + ML (Isolation Forest) anomaly flagging.
- Directory: `services/anomaly-detection`. Port 8003.
- Start: `docker compose up -d anomaly-detection`
- Health: `curl -sf http://localhost:8003/health`
- Endpoints: `GET /anomalies`, `GET /anomalies/{event_id}`, `POST /anomalies/evaluate`.
- Consumes: `ais.features` (group `anomaly-detection`). Produces: `anomaly.events`.
- DB: `vessels`, `vessel_features`, `anomaly_events`, `environmental_conditions`.
- Config: `AIS_GAP_THRESHOLD_MINUTES`, `ERRATIC_COURSE_VARIANCE`, `LOITERING_THRESHOLD`,
  `ML_MODEL_DIR` (volume `ml-models` at `/data/models`), `ML_TRAIN_INTERVAL_S`,
  `ML_MAX_MODEL_AGE_H`, `ML_MIN_SAMPLES`.
- Logs: `docker compose logs -f anomaly-detection`

### 10.7 ais-spoof-detection

- Purpose: dead-reckoning trust scoring to flag spoofing.
- Directory: `services/ais-spoof-detection`. Port 8005.
- Start: `docker compose up -d ais-spoof-detection`
- Health: `curl -sf http://localhost:8005/health`
- Endpoints: `GET /vessels/{mmsi}/trust-score`, `GET /spoofing-suspects`.
- Consumes: `ais.clean` (group `spoof-detection`). Produces: `ais.trust`.
- DB: `ais_trust_scores`. Config: `MAX_PLAUSIBLE_AIS_ERROR_M`, `MAX_PHYSICAL_SPEED_MS`.

### 10.8 sts-detection

- Purpose: detects ship-to-ship (STS) encounters from paired vessel tracks.
- Directory: `services/sts-detection`. Port 8006.
- Start: `docker compose up -d sts-detection`
- Health: `curl -sf http://localhost:8006/health`
- Consumes: `ais.features` (group `sts-detection`). Produces: `sts.events`.
- DB: `sts_events`. Config: `STS_DISTANCE_THRESHOLD_M` (500), `STS_SPEED_THRESHOLD_KN`
  (2.0), `STS_MIN_DURATION_MIN` (30).

### 10.9 vessel-risk-engine

- Purpose: computes per-vessel risk score + tier + recommended action.
- Directory: `services/vessel-risk-engine`. Port 8007.
- Start: `docker compose up -d vessel-risk-engine`
- Health: `curl -sf http://localhost:8007/health`
- Consumes: `anomaly.events`, `ais.trust`, `sts.events` (group `risk-engine`). Produces:
  `vessel.risk`.
- DB: `vessel_risk_scores`, `satellite_tasking_requests`. Config: `RISK_DECAY_RATE`,
  `RISK_TIER_MEDIUM/HIGH/CRITICAL`.

### 10.10 dark-vessel-detection

- Purpose: SAR-driven dark-vessel detection (consumes `sar.clean`).
- Directory: `services/dark-vessel-detection`. Port 8004.
- Start: `docker compose up -d dark-vessel-detection`
- Health: `curl -sf http://localhost:8004/health`
- Consumes: `sar.clean` (group `dark-vessel-detection`). Produces: `dark.vessel.events`.
- DB: `vessel_positions`, `dark_vessel_events`.
- Config: `DARK_VESSEL_GAP_MINUTES` (90), `DARK_VESSEL_SCAN_INTERVAL_S` (300),
  `DARK_VESSEL_MAX_MATCH_DISTANCE_M` (5000), `DARK_VESSEL_MAX_MATCH_TIME_S` (1800).

### 10.11 sar-spill-intelligence

- Purpose: consumes `sar.clean`, runs Lee despeckle → Otsu + CFAR → morphology →
  polygonize, writes `spill_candidates` + scene artifact bundle, publishes
  `spill.candidates.raw`.
- Directory: `services/sar-spill-intelligence`. Port 8008.
- Start: `docker compose up -d sar-spill-intelligence`
- Health: `curl -sf http://localhost:8008/health` (see `scenes_processed`,
  `scenes_failed`); Status: `curl -sf http://localhost:8008/status`.
- Consumes: `sar.clean` (group `sar-spill-intelligence`). Produces:
  `spill.candidates.raw`.
- DB: `spill_candidates`. Artifact volume: `sar-scene-artifacts` at
  `/data/artifacts` (`SAR_ARTIFACT_ROOT`). Requires rasterio/GDAL runtime libs (installed
  in its Dockerfile).
- Config: **`SAR_MIN_AREA_M2` and `SAR_BRIGHT_TARGET_THRESHOLD` must be set or Step 3
  fails fast**; `SAR_ARTIFACT_ROOT`.
- Logs: `docker compose logs -f sar-spill-intelligence`
- Expected startup: `sar-spill-intelligence worker started`, then per-event processing
  logs like `Processed SAR scene id=... candidates=N artifact=...`.
- Troubleshooting: "configuration error / missing SAR_MIN_AREA_M2" ⇒ set both thresholds
  (Section 11).

### 10.12 lookalike-engine

- Purpose: Step 4 shape heuristics (ship-shadow / calm-water / possible_slick) + Step 5
  GLCM texture from **raw** SAR + heuristic confidence; updates `spill_candidates` and
  publishes `spill.candidates.filtered` for `possible_oil_spill` only.
- Directory: `services/lookalike-engine`. Port 8009.
- Start: `docker compose up -d lookalike-engine`
- Health: `curl -sf http://localhost:8009/health`; Status: `/status`.
- Consumes: `spill.candidates.raw` (group `lookalike-engine`). Produces:
  `spill.candidates.filtered`.
- DB: `spill_candidates`. Artifact volume: `sar-scene-artifacts` (`SAR_ARTIFACT_ROOT`).
- Config: `SAR_ARTIFACT_ROOT`.
- Logs: `docker compose logs -f lookalike-engine`
- Troubleshooting: `candidates_skipped` increments when the raw scene artifact is missing
  (e.g. scenes processed before Step 5 existed — Section 23).

### 10.13 evidence-fusion

- Purpose: fuses `possible_oil_spill` candidates with high-risk vessels
  (tier HIGH/CRITICAL) via PostGIS geography `ST_DWithin` + temporal window, publishes
  `incident.fused`. Fusion only — never source attribution.
- Directory: `services/evidence-fusion`. Port 8010.
- Start: `docker compose up -d evidence-fusion`
- Health: `curl -sf http://localhost:8010/health`; Status: `/status`.
- Consumes: `vessel.risk` (drained/counted) + `spill.candidates.filtered` (group
  `evidence-fusion`). Produces: `incident.fused`.
- DB: `spill_candidates`, `vessel_risk_scores`, `vessel_positions`.
- Config: `EVIDENCE_SPATIAL_WINDOW_M` (5000), `EVIDENCE_TEMPORAL_WINDOW_HOURS` (6).
- Logs: `docker compose logs -f evidence-fusion`
- Expected log: `fused candidate=<id> correlated_vessel_id=<id|null>`.

### 10.14 api-gateway

- Purpose: unified REST API + WebSocket `/live` relay for the dashboard.
- Directory: `services/api-gateway`. Port 8015.
- Start: `docker compose up -d api-gateway`
- Health: `curl -sf http://localhost:8015/health`; Aggregate:
  `curl -sf http://localhost:8015/system/health`.
- System: `GET /system/pipeline`, `GET /system/stats`.
- Vessels: `GET /vessels`, `GET /vessels/{mmsi}`, `/vessels/{mmsi}/track`,
  `/vessels/{mmsi}/anomalies`, `/vessels/{mmsi}/trust`, `/vessels/{mmsi}/risk`,
  `/vessels/risk/leaderboard`.
- Events: `GET /anomalies`, `GET /sts`, `GET /sts/active`,
  `GET /spoofing/suspects`, `GET /risk/vessels`, `GET /risk/tasking-requests`,
  `GET /features`.
- Spill: `GET /spill/candidates/{candidate_id}` (GeoJSON geometry + full row).
- Live: `GET ws://localhost:8015/live` — pushes `anomaly`, `sts`, `risk`,
  `spill_candidate`, `incident_fused`, plus a 10 s heartbeat.
- Logs: `docker compose logs -f api-gateway`
- Expected startup: `API Gateway started`, `Alert started`.
- Troubleshooting: `system/health` shows a service DOWN → check that service's own port
  and logs.

### 10.15 dashboard

- Purpose: ACTIVE frontend (React + Vite + Leaflet); connects to api-gateway REST +
  `/live` WS; renders vessels, anomalies and spill candidates.
- Directory: `dashboard/`. Port 3000 (host and container; nginx).
- Start (Docker): `docker compose up -d --build dashboard`
- Dev (host): `cd dashboard && npm install && npm run dev` (Vite on :3000).
- Build only: `cd dashboard && npm run build`.
- Health: open `http://localhost:3000`; WebSocket = `ws://localhost:8015/live`
  (`defaultWsUrl()` uses port 8015 when the page is served on :3000).
- Logs: `docker compose logs -f dashboard`
- Troubleshooting: "dashboard not receiving events" → api-gateway must be up and the
  browser must connect to `:8015` (Section 20).

### 10.16 simulator

- Purpose: data replay container; the current `main.py` only prints
  "simulator ready, waiting for data files in /data" and sleeps (no active publisher).
- Directory: `simulator/`. No port, no HTTP.
- Start: `docker compose up -d simulator` (mounts `./data` read-only at `/data`).
- Logs: `docker compose logs -f simulator`
- Expected output: the single "simulator ready…" line repeating.
- **Note:** as shipped, the simulator does **not** push data into the pipeline. For live
  AIS data use the real AIS feed / `ais-reader` / `POST /ingest/ais`.

### 10.17 source-attribution, drift-forecast, severity-impact, response-decision

- These four are **heartbeat-only skeleton services** (their `main.py` runs a 5 s ticker
  and answers `/health`; no worker logic is wired). Ports 8011–8014.
- Start each: `docker compose up -d source-attribution drift-forecast severity-impact response-decision`
- Health: `curl -sf http://localhost:8011/health` (and 8012/8013/8014).
- They do **not** consume or publish streams yet, and write no pipeline tables
  (`seed_demo_data.py` populates their legacy tables directly).

---

## 11. SAR Pipeline

### 11.1 The processing chain (as implemented)

```
data-ingestion        (SAR trigger / synthetic injection)   → SAR acquisition (GEE) or inject synthetic
   │  sar.clean (GeoTIFF path + scene_metadata incl. is_synthetic)
   ▼
sar-spill-intelligence
   ├─ Lee despeckle            (app/despeckle.py)
   ├─ Otsu dark-region segmentation  ∪  CFAR small-target detection   (app/segmentation.py + cfar.py)
   ├─ morphology: cleaned_mask (binary open 3 / close 5, min_area filter)   (app/morphology.py)
   ├─ polygonize → candidates (lon/lat GeoJSON)   (app/polygonize.py)
   ├─ save scene artifact bundle to /data/artifacts/<scene_id>
   ├─ persist spill_candidates (PostGIS; area via geography)   (app/worker.py)
   └─ publish spill.candidates.raw (one event per scene)
   ▼
lookalike-engine
   ├─ Step 4 shape heuristics → likely_ship_shadow | likely_calm_water | possible_slick   (app/shape_filters.py)
   └─ Step 5 GLCM texture (raw_image.npy) + heuristic scoring   (app/texture.py + scoring.py)
        → possible_oil_spill | low_confidence
   └─ spill.candidates.filtered (only possible_oil_spill)
   ▼
evidence-fusion  → vessel correlation → incident.fused
```

### 11.2 Thresholds and constants (validated vs tunable vs fixture)

| Value | Meaning | Marking |
|---|---|---|
| `SAR_MIN_AREA_M2` | Minimum physical spill area; Step 3 fails fast if unset/invalid. | **TUNABLE — NOT VALIDATED.** `SAR_DEMO.md` uses `5000` only as an example. |
| `SAR_BRIGHT_TARGET_THRESHOLD` | High-backscatter threshold for the bright-target mask (`filtered_image > threshold`). | **TUNABLE — NOT VALIDATED.** Example `18.0`. |
| Test fixtures | `SAR_MIN_AREA_M2=20000`, threshold `1.0`, synthetic inject `length_px=40,width_px=8,angle_deg=30,darkness_db=-6`. | **TEST FIXTURE ONLY** — never production-calibrated. |

There is **no** hardcoded default for the two SAR thresholds — they are read from the
environment and the worker fails fast if they are absent, so a test value is never
silently reused as a validated one.

### 11.3 Artifact storage (`sar-scene-artifacts` volume)

Raster pixels are **never** stored in PostgreSQL. One bundle per scene is written to the
named volume `sar-scene-artifacts`, mounted at `SAR_ARTIFACT_ROOT` (default
`/data/artifacts`) in `data-ingestion`, `sar-spill-intelligence` and `lookalike-engine`:

```text
<SAR_ARTIFACT_ROOT>/<scene_id sanitized>/
    metadata.json            affine transform, shape, crs, scene_id
    raw_image.npy           pre-despeckle backscatter (float)   ← Step 5 GLCM source
    filtered_image.npy      Lee-filtered backscatter (float)
    cleaned_mask.npy        morphologically cleaned candidate mask (bool)
    bright_target_mask.npy  high-backscatter targets (bool)
```

- Mapping is `candidate_id → scene_id → <scene_id>/`; the sanitised `scene_id` directory
  name is the key (no DB index for artifacts).
- GLCM **must** read `raw_image.npy` (pre-despeckle); the Lee filter removes the texture
  detail that Step 5 needs.
- Pixel mapping uses the **inverse** of the stored affine transform: world X (longitude)
  → column, world Y (latitude) → row; crops are clamped to the raster extent.

### 11.4 Why geometry uses lon/lat

Every spatial column is `GEOMETRY(...,4326)` with points as `POINT(longitude latitude)`
(`docs/spatial.md`). Metric math (area, `ST_DWithin`) goes through the geography cast so
degrees are never treated as metres. `SAR_MIN_AREA_M2` is translated to a pixel threshold
by `estimate_min_area_px`, and each stored `area_m2` is
`ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(..),4326)::geography)`.

### 11.5 Enabling the SAR pipeline

- **Real imagery (Run A):** requires GEE creds (§6) + Sentinel-1 coverage over the Mumbai
  AOI in the requested date window.
- **Synthetic (Run B):** does **not** require GEE (Section 14).
- Both need `sar-spill-intelligence`, `lookalike-engine`, `evidence-fusion` running and
  `SAR_MIN_AREA_M2` + `SAR_BRIGHT_TARGET_THRESHOLD` set.

---

## 12. AIS / Vessel-Risk Pipeline

### 12.1 Ingestion

Live AIS enters through `ais-reader` (WebSocket to AISStream / HTTP poll to VesselAPI),
`data-ingestion`'s `POST /ingest/ais`, or (in principle) the simulator. Validated records
are normalised, written to `vessels` + `vessel_positions`, and published to `ais.clean`.

### 12.2 Processing and scoring chain

- `ais-analytics` builds 15-min feature windows → `ais.features` + `vessel_features`.
- `anomaly-detection` flags anomalies (rules + z-score + Isolation Forest) →
  `anomaly.events` + `anomaly_events`.
- `ais-spoof-detection` scores dead-reckoning trust → `ais.trust` + `ais_trust_scores`.
- `sts-detection` detects ship-to-ship encounters → `sts.events` + `sts_events`.
- `vessel-risk-engine` consumes `anomaly.events`, `ais.trust`, `sts.events`, computes a
  risk tier, publishes `vessel.risk`, and can write `satellite_tasking_requests`.
- `dark-vessel-detection` runs independently on `sar.clean` → `dark.vessel.events` +
  `dark_vessel_events` (SAR-detected vessels with no AIS).

### 12.3 Risk semantics

Tier enum is `LOW / MEDIUM / HIGH / CRITICAL` (`risk_tier_enum`). Thresholds default
`RISK_TIER_MEDIUM=20`, `RISK_TIER_HIGH=50`, `RISK_TIER_CRITICAL=75`. `HIGH`/`CRITICAL`
are the tiers `evidence-fusion` and the api-gateway treat as "high-risk".

### 12.4 Evidence-fusion correlation (also see §13)

`evidence-fusion` correlates a SAR `possible_oil_spill` candidate with high-risk vessels:

- **Spatial:** PostGIS geography `ST_DWithin(candidate_centroid, vessel_position,
  spatial_window_m)` — metres, never degree differences.
- **Temporal:** vessel position within
  `[acquisition_time − window, acquisition_time + window]`.
- **Windows (tunable, NOT validated scientific constants):**
  `EVIDENCE_SPATIAL_WINDOW_M` (default `5000` m), `EVIDENCE_TEMPORAL_WINDOW_HOURS`
  (default `6` h).
- Multi-vessel selection: nearest geographic distance; ties broken by lowest `mmsi`.

> **`correlated_vessel_id` ≠ source attribution.** A correlated vessel is contextual
> evidence that a vessel-risk record was spatially/temporally near the candidate — it
> does **not** mean that vessel caused the spill. No fusion event emits a
> source-attribution field (`caused_by`, `responsible_vessel`, `source_vessel`,
> `attribution`).

### 12.5 Synthetic provenance

`is_synthetic` is carried unchanged through the whole SAR flow (`sar.clean` →
`spill_candidates` → `spill.candidates.raw` → `spill.candidates.filtered` →
`incident.fused`). It is provenance only and never changes classification or confidence.

---

## 13. Evidence Fusion

`evidence-fusion` (`services/evidence-fusion`) is the SAR + AIS/context bridge. It:

1. consumes `spill.candidates.filtered` (`possible_oil_spill`) and `vessel.risk`
   (drained/counted),
2. resolves the candidate centroid + acquisition time from `spill_candidates` in PostGIS,
3. queries high-risk vessels (tier `HIGH`/`CRITICAL`) via geography `ST_DWithin` within
   the configured windows,
4. selects the deterministically nearest correlated vessel (nullable),
5. publishes one `incident.fused` event per candidate.

When no vessel qualifies, `correlated_vessel_id`/`correlated_vessel` are `null` and the
candidate is still published (absence of vessel context is itself evidence).
`incident.fused` fields: `candidate_id`, `scene_id`, `confidence`,
`classification_label`, `acquisition_time`, scene metadata, `is_synthetic`, plus
`correlated_vessel_id`/`correlated_vessel` (mmsi/risk_score/tier/recommended_action/
distance_m/position_timestamp). It never carries attribution fields.

---

## 14. Synthetic Demo

### 14.1 Purpose

A **controlled, opt-in** validation/demo mechanism (Step 7) to exercise the full real
pipeline deterministically without depending on GEE. An explicit synthetic dark slick is
injected into a raw SAR raster and the `is_synthetic` provenance flag is asserted at
every boundary.

**Triggering controls (redundant by design):**

- env `INJECT_SYNTHETIC` (`true`/`false`, default `false`) — used when no explicit flag is
  given at acquisition time.
- `inject_synthetic=true` / `--run-b` / the explicit `inject_synthetic` argument on the
  SAR trigger — takes **precedence** over `INJECT_SYNTHETIC`.
- Injection writes a **new** `<stem>_synthetic.tif` (the original raster is **never**
  overwritten) and stamps `is_synthetic=true` in the `sar.clean` scene metadata. It is
  **disabled by default** (never silently enabled).

> ⚠️ **Synthetic output is NOT a real oil-spill detection.** A Run B `possible_oil_spill`
> result demonstrates that the pipeline and provenance work; it does **not** mean oil was
> detected. The dashboard draws synthetic candidates with a dashed border and a
> "SYNTHETIC DEMO" popup so they are never mistaken for real (Section 15).

### 14.2 The demo script (`scripts/demo_sar_spill.py`)

Run **inside the `data-ingestion` container** so it reuses that container's GEE creds (if
any) and network access:

```bash
docker compose cp scripts/demo_sar_spill.py data-ingestion:/app/demo_sar_spill.py
docker compose exec data-ingestion python /app/demo_sar_spill.py
```

**Arguments:**
- `--timeout N` — whole-pipeline settle timeout, **default 1800** s.
- `--start YYYY-MM-DD`, `--end YYYY-MM-DD` — date range (default 14 days ago → now).
- `--run-a` — real imagery. `--run-b` — synthetic. Neither flag ⇒ **both** runs.
- `--verbose`.

**Run A — real Sentinel-1:** queries GEE, exports the best VV scene to Drive, downloads
the GeoTIFF to `/data/artifacts/sar/`, publishes `sar.clean`, waits for the pipeline and
reports candidates. Output may legitimately be **0 candidates** if no dark targets exist
over the window.

**Run B — synthetic validation:** same acquisition flow with `inject_synthetic=True`;
asserts `is_synthetic == True` at `sar.clean`, `spill_candidates`,
`spill.candidates.filtered`, and `incident.fused`, and that a synthetic candidate reaches
fusion. It exits non-zero if provenance is lost.

**Expected output:** `RUN A — REAL SENTINEL-1 IMAGERY` / `RUN B — CONTROLLED SYNTHETIC
VALIDATION` banners, per-boundary candidate counts, and a `provenance (is_synthetic==…)`
OK/MISMATCH block.

**Requirements:** `SAR_MIN_AREA_M2` + `SAR_BRIGHT_TARGET_THRESHOLD` set in `.env`, and
`sar-spill-intelligence` + `lookalike-engine` + `evidence-fusion` running. Run A also
needs GEE creds (§6); Run B does **not** need a GEE key.

Unit test for the script: `python3 -m pytest scripts/test_demo_sar_spill.py`.

---

## 15. Dashboard

**Active frontend: `dashboard/`.** It is the only frontend referenced by
`docker-compose.yml` (service `dashboard`, host port `3000`). `frontend/` is a **stale
first-draft** (maplibre-based, TypeScript, separate `package.json`); it is **not** in
`docker-compose.yml`, has no tests, and is **not** a supported route.

- Stack: React 18 + Vite + **Leaflet** (`react-leaflet`), served by **nginx** in the
  Docker image (`nginx:alpine`, `listen 3000`).
- Build (Docker): `docker compose build dashboard` and
  `docker compose up -d dashboard`.
- Build (host): `cd dashboard && npm install && npm run build` (outputs `dist/`).
- Dev server (host): `cd dashboard && npm install && npm run dev` → Vite on `:3000`.
- Test (host): `cd dashboard && npm run test` (vitest; unit tests for `spillEvents.js`,
  `SpillCandidateLayer`, `useLiveEvents` hook).
- API gateway dependency: the dashboard reads candidate geometry/records from
  `api-gateway` REST (`GET /spill/candidates/{id}`) and subscribes to its WebSocket.
- **WebSocket endpoint:** `ws://<hostname>:8015/live`. `defaultWsUrl()` uses
  `VITE_WS_URL` if set, otherwise `ws(s)://<hostname>:<port>/live` where the port is
  `8015` when the page is served on `:3000` (i.e. development and the Docker build).
- Leaflet map: represents live vessels (`VesselLayer`), anomalies, and spill candidates
  (`SpillCandidateLayer`).
- Spill candidate visualization: one **GeoJSON polygon per `candidate_id`** resolved from
  `GET /spill/candidates/{id}` (WS events carry only scalars — geometry is never
  fabricated). Both `spill.candidates.filtered` and `incident.fused` merge into the same
  candidate record.
- Confidence colouring: linear **yellow → red** over `[0.5, 1.0]` (`confidenceColor`),
  fallback yellow when confidence is missing.
- Synthetic styling: `is_synthetic` candidates get a **dashed border**, higher weight,
  lower fill opacity, and a `SYNTHETIC DEMO` banner in the popup (`candidateStyle`).
- Popup fields: Classification, Confidence, Area (m²), Acquisition time, Correlated
  vessel, plus raw GLCM texture when available.

---

## 16. Start Everything

There is **no** single "one-command" target that reproducibly runs the full stack ready
for the SAR pipeline (the `Makefile` `up`/`up-ais` start services but do not apply
migrations or seed GEE). Use the sequence below. It matches what this repository supports.

```bash
# 1. Configure environment
cp .env.example .env            # then edit as needed (Section 5)

# 2. Add the GEE key (needed ONLY for real SAR / demo Run A; Section 6)
mkdir -p secrets
#   cp /path/to/your-key.json secrets/gee-key.json

# 3. Build images (first build is slow — pulls base images, GDAL, node)
docker compose build

# 4. Start infrastructure
docker compose up -d postgres redis

# 5. Apply the SAR migrations (idempotent)
cat infra/postgres/migrate_stage2.sql | docker compose exec -T postgres psql -U postgres -d maritime_oilspill
cat infra/postgres/migrate_stage3.sql | docker compose exec -T postgres psql -U postgres -d maritime_oilspill
cat infra/postgres/migrate_stage4.sql | docker compose exec -T postgres psql -U postgres -d maritime_oilspill
cat infra/postgres/migrate_stage5.sql | docker compose exec -T postgres psql -U postgres -d maritime_oilspill

# 6. Start the application services
docker compose up -d data-ingestion ais-reader ais-analytics anomaly-detection \
                     ais-spoof-detection sts-detection vessel-risk-engine \
                     sar-spill-intelligence lookalike-engine evidence-fusion \
                     source-attribution drift-forecast severity-impact response-decision \
                     api-gateway dashboard

# 7. Verify health (Section 17)
curl -sf http://localhost:8015/system/health | python3 -m json.tool
```

Alternatively `make up` starts **all** compose services (equivalent to
`docker compose up -d`); `make up-ais` starts just the AIS phase
(`postgres redis data-ingestion ais-analytics anomaly-detection ais-spoof-detection
sts-detection vessel-risk-engine api-gateway ais-reader`).

> The SAR migration stages 2–5 must be applied manually — `docker-compose.yml` does not
> run them. On a **fresh** `pgdata` volume `infra/postgres/init.sql` already contains the
> full `spill_candidates` schema + enum, so the migrations are primarily for existing
> volumes; applying them is always safe because they are idempotent.

---

## 17. Verify Everything Is Running

Expected healthy states after Section 16.

```bash
# Containers
docker compose ps                       # all services: Up; postgres/redis: Up (healthy)

# PostgreSQL reachable + user/db correct
docker compose exec postgres pg_isready -U postgres -d maritime_oilspill   # → "/var/run/postgresql:5432 - accepting connections"

# PostGIS installed
docker compose exec postgres psql -U postgres -d maritime_oilspill -c "SELECT PostGIS_Version();"

# Important tables exist (should list many, incl. spill_candidates)
docker compose exec postgres psql -U postgres -d maritime_oilspill -c "\dt"

# Redis
docker compose exec redis redis-cli ping    # PONG

# Every FastAPI health endpoint (host ports)
for p in 8001 8002 8003 8004 8005 8006 8007 8008 8009 8010 8011 8012 8013 8014 8015; do
  echo -n ":${p}  "; curl -sf "http://localhost:${p}/health" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status'),'worker_alive=',d.get('worker_alive'))" || echo DOWN
done

# API gateway high-level
curl -sf http://localhost:8015/system/health | python3 -m json.tool
curl -sf http://localhost:8015/system/pipeline | python3 -m json.tool   # stream lengths + consumer groups
curl -sf http://localhost:8015/system/stats | python3 -m json.tool

# Dashboard
curl -sI http://localhost:3000 | head -1    # 200 OK

# WebSocket (handshake) — expect an Upgrade/101 or JSON heartbeat frames
# (e.g. with a WS client: ws://localhost:8015/live)

# Redis streams present (SAR + AIS)
docker compose exec redis redis-cli XLEN sar.clean
docker compose exec redis redis-cli XLEN spill.candidates.raw
docker compose exec redis redis-cli XLEN spill.candidates.filtered
docker compose exec redis redis-cli XLEN incident.fused
docker compose exec redis redis-cli XLEN ais.clean

# Artifact volume is mounted (dir exists named after a processed scene)
docker run --rm -v <project>_sar-scene-artifacts:/data/artifacts alpine ls -la /data/artifacts
```

**Expected healthy:** `worker_alive: true` everywhere; `/system/health` lists all services
`up`/`ok`; all `XLEN` values ≥ 0 (0 is valid for idle streams); the dashboard returns
`200`; the WS connection accepts and emits a `heartbeat` frame within ~10 s.

---

## 18. Run an End-to-End Demo

Full end-to-end (prereqs: Section 6, 16, 17 — SAR thresholds set, SAR services running).

1. **Start infrastructure + services:** follow Section 16.
2. **Verify health:** Section 17 (especially `8008`, `8009`, `8010` alive).
3. **Use the simulator or a synthetic SAR fixture instead of live GEE for a runnable
   demo.** `docs/sar-spill-pipeline.md` documents a deterministic fixture generator:
   ```bash
   python3 scripts/generate_synthetic_sar_fixture.py data/sample_sar/synthetic_e2e.tif
   ```
   (You must also arrange for that raster to be published on `sar.clean`; the repository's
   supported reproducible path is the **demo script** below, which drives acquisition.)
4. **Synthetic validation (no GEE needed):**
   ```bash
   docker compose cp scripts/demo_sar_spill.py data-ingestion:/app/demo_sar_spill.py
   docker compose exec data-ingestion python /app/demo_sar_spill.py --run-b
   ```
5. **Watch processing logs in live terminals:**
   ```bash
   docker compose logs -f sar-spill-intelligence
   docker compose logs -f lookalike-engine
   docker compose logs -f evidence-fusion
   docker compose logs -f api-gateway
   ```
6. **Inspect candidates:** 
   ```bash
   docker compose exec postgres psql -U postgres -d maritime_oilspill -c \
     "SELECT candidate_id, scene_id, status, confidence, classification_label, is_synthetic FROM spill_candidates ORDER BY created_at DESC LIMIT 20;"
   ```
7. **Inspect evidence fusion:** confirm `incident.fused` in Redis:
   `docker compose exec redis redis-cli XLEN incident.fused` and view an entry via
   `docker compose exec redis redis-cli XRANGE incident.fused - + COUNT 1`.
8. **Open the dashboard:** `http://localhost:3000`; synthetic candidates appear dashed.
9. **Real SAR (Run A)** — only with GEE creds + Sentinel-1 coverage:
   ```bash
   docker compose exec data-ingestion python /app/demo_sar_spill.py --run-a --start 2024-01-01 --end 2024-01-31
   ```

For live AIS demo without an API key, push records manually:
```bash
curl -X POST http://localhost:8001/ingest/ais -H 'Content-Type: application/json' -d '{"mmsi":123456789,"lat":18.5,"lon":72.9,"speed_knots":10,"course":120,"timestamp":"2024-01-01T00:00:00Z","vessel_name":"Demo"}'
```
then watch `ais.clean` and downstream: `docker compose exec redis redis-cli XLEN ais.clean`,
`docker compose exec redis redis-cli XLEN vessel.risk`.

---

## 19. Testing

Each service directory that has unit tests ships them next to the `app` package (a
package named `app`), so they must be run **per suite** (pytest `testsdir` separation),
not from the repo root as one glob. Provenance tests stub `shared.*` / `rasterio` via
`sys.modules`, so they need no live Redis/PostGIS. A PostGIS-geography test skips when
PostgreSQL is unreachable (the `tests/test_spatial.py` pattern).

| Component | Command (from repo root) | Expected result |
|---|---|---|
| Data ingestion (synthetic injection) | `python3 -m pytest services/data-ingestion/test_synthetic_injection.py -v` | 10/10 pass |
| SAR (CFAR/despeckle/segmentation/morphology/polygonize/provenance) | `python3 -m pytest services/sar-spill-intelligence/ -v` | 28/28 pass |
| Lookalike (shape filters, artifact mapping, texture, scoring, provenance) | `python3 -m pytest services/lookalike-engine/ -v` | 37/37 pass |
| Evidence fusion (correlation, fusion, provenance) | `python3 -m pytest services/evidence-fusion/ -v` | 19/19 pass |
| Demo script helpers | `python3 -m pytest scripts -v` | passes (`test_demo_sar_spill.py`) |
| Spatial regression (km/m, ST_DWithin, topology, lon/lat) | `python3 -m pytest tests/ -v` | passes; the DB-backed geography test **skips** if Postgres is not reachable |
| Dashboard (vitest) | `cd dashboard && npm test` | passes (`spillEvents.test.js`, `SpillCandidateLayer.test.jsx`) |

**Dependency note:** the test counts (28/28, 37/37, 19/19, 10/10) are as reported by the
project (`docs/sar-spill-pipeline.md` §12). The service test dirs each define their own
`requirements.txt`. To run them on the host you must install the relevant
`services/<name>/requirements.txt` plus `pytest`. Per README, the spatial suite can also
be run against a live Postgres:

```bash
docker run --rm --network aqua-sentinal_aqua-net \
  -e POSTGRES_HOST=postgres -e POSTGRES_DB=aqua_sentinel \
  -e POSTGRES_USER=aqua_sentinel -e POSTGRES_PASSWORD=change_me \
  -v "$PWD":/work -w /work python:3.11-slim \
  sh -c "pip install -q -r tests/requirements.txt && python -m pytest tests/ -v"
```

**Environment-dependent tests:** any test that touches PostGIS will **skip** (not fail)
when PostgreSQL is unavailable — that is intentional; the geography-cast area test needs
a live PostGIS server. The SAR/lookalike/evidence/provenance tests avoid live infra by
design.

---

## 20. Troubleshooting

Every issue uses: **SYMPTOM → CAUSE → CHECK → FIX** (no destructive commands unless
clearly labelled).

### 20.1 Docker build failures

- **SYMPTOM:** `docker compose build` fails on GDAL/`libgdal`/`rasterio` errors.
- **CAUSE:** bad cache or platform mismatch for the GDAL wheel.
- **CHECK:** `docker compose build --no-cache sar-spill-intelligence data-ingestion`;
  `docker run --rm <image> python -c "import rasterio"`.
- **FIX:** rebuild with `--no-cache`; keep `platform: linux/amd64` on Postgres; the
  Dockerfiles already install the required GDAL libs — don't strip them.

### 20.2 PostgreSQL port conflict

- **SYMP:** "port is already allocated" for host 5433.
- **CAUSE:** a local Postgres/PostGIS already listens on 5433.
- **CHECK:** `lsof -iTCP:5433 -sTCP:LISTEN`.
- **FIX:** change the host mapping to e.g. `5434:5432` in `docker-compose.yml` and update
  host-side commands (container/internal port stays 5432; containers use `postgres:5432`).

### 20.3 Redis issues

- **SYMP:** services log `Redis connection failed` and stay down.
- **CAUSE:** Redis not up, or `REDIS_HOST` wrong.
- **CHECK:** `docker compose ps redis`; `docker compose exec redis redis-cli ping`.
- **FIX:** `docker compose up -d redis`; keep `REDIS_HOST=redis` (use service names, not
  `localhost`, inside containers).

### 20.4 `shared/` vs `services/shared/` packaging conflicts

- **SYMP:** `ModuleNotFoundError: No module named 'shared.db'` / `'shared.spatial'`
  (or the wrong `shared.db`) in a SAR service.
- **CAUSE:** two shared packages exist — top-level `shared/db/` (**package**) and
  `services/shared/db.py` (**module**). Each image/compose mount selects one.
- **CHECK:** `docker compose exec <svc> sh -c "ls /app/shared; python -c 'import
  shared.db,inspect;print(inspect.getfile(shared.db))'"`.
- **FIX:** use the per-service compose volumes exactly as declared; never add a generic
  `./services/shared:/app/shared` mount to a SAR service. Rebuild the affected image.

### 20.5 GDAL / rasterio errors

- **SYMP:** `rasterio.Error` / "GDAL 3.x required" at SAR startup.
- **CAUSE:** missing GDAL runtime libraries.
- **CHECK:** `docker compose logs sar-spill-intelligence | tail`;
  `docker compose exec sar-spill-intelligence python -c "import rasterio"`.
- **FIX:** use the shipped image (its Dockerfile installs `libgdal-dev libexpat1 libcurl4
  libpng16-16 libjpeg62-turbo libopenjp2-7 libgeos-c1v5`).

### 20.6 GEE credentials / missing GEE key

- **SYMP:** data-ingestion logs an auth error; `secrets/gee-key.json` missing.
- **CAUSE:** `GEE_SERVICE_ACCOUNT` empty, key not at host `./secrets/gee-key.json`, or the
  service account not registered for Earth Engine.
- **CHECK:** `ls -la secrets/`; in container `ls -la /run/secrets/gee-key.json`; check the
  env value.
- **FIX:** complete Section 6 steps 1–7, then `docker compose up -d --force-recreate
  data-ingestion`.

### 20.7 Earth Engine export/download failures

- **SYMP:** demo Run A times out on export/Drive download.
- **CAUSE:** no Sentinel-1 scene in window/AOI, Drive API not enabled, export volume, or
  the 600 s default wait exceeded.
- **CHECK:** `docker compose logs data-ingestion | grep -iE "ee|drive"`.
- **FIX:** widen the date range; enable Drive API; register the SA; raise the export
  `max_wait` in `sar_acquisition.py` if needed (`docs/SAR_DEMO.md`).

### 20.8 SAR raster missing / no `spill_candidates` rows

- **SYMP:** `sar.clean` published but `scenes_failed` or 0 candidates.
- **CAUSE:** GeoTIFF path unreadable by the container, no finite VV pixels, or thresholds
  unset (Step 3 fail-fast).
- **CHECK:** both thresholds set; `docker compose logs -f sar-spill-intelligence`.
- **FIX:** point the artifact volume correctly; set both thresholds; a real scene with no
  dark targets legitimately yields 0.

### 20.9 Redis stream consumer issues

- **SYMP:** a worker logs consume-loop errors, or messages pile up unprocessed.
- **CAUSE:** consumer group created while the stream was empty, a consumer crashed before
  ack, or an unhandled message error.
- **CHECK:** `docker compose exec redis redis-cli XINFO GROUPS <stream>`; watch the
  worker's counters.
- **FIX:** the shared helpers auto-ack each delivery and catch broadly; if lag persists,
  `docker compose restart <svc>`.

### 20.10 WebSocket issues

- **SYMP:** WS `/live` won't connect; browser connection error.
- **CAUSE:** api-gateway not running, or wrong URL/port (browser must target `:8015`).
- **CHECK:** `docker compose ps api-gateway`; `curl -sI http://localhost:8015/health`.
- **FIX:** start api-gateway; in dev run the dashboard on `:3000` (or set `VITE_WS_URL`)
  so `defaultWsUrl()` selects `:8015`.

### 20.11 Dashboard not receiving events

- **SYMP:** blank map / no live updates.
- **CAUSE:** api-gateway up but no data flowing, or wrong WS port.
- **CHECK:** browser console for an upgrade error; `docker compose exec redis redis-cli
  XLEN incident.fused`.
- **FIX:** confirm the streams have data (0 is valid when idle); fix the WS URL (20.10);
  keep api-gateway running.

### 20.12 PostGIS geography errors

- **SYMP:** `ST_DWithin`/`ST_Distance` returns nonsense or a cast error.
- **CAUSE:** geometry degrees treated as metres, or SRID mismatch.
- **CHECK:** columns are `GEOMETRY(...,4326)`; queries use `::geography` for meters.
- **FIX:** follow `docs/spatial.md`: geography cast for metric math, `POINT(longitude
  latitude)`, never change geometry columns to geography.

### 20.13 Migration issues

- **SYMP:** `spill_candidates`/enum missing or "value already exists".
- **CAUSE:** migrations not run, or run out of order.
- **CHECK:** `docker compose exec postgres psql -U postgres -d maritime_oilspill -c "\d
  spill_candidates"`; `\dT+ spill_candidate_status_enum`.
- **FIX:** apply stages 2→3→4→5 in order (idempotent); restart the SAR services
  afterwards.

### 20.14 Stale Docker volumes

- **SYMP:** stale behavior after rebuild (missing columns, old artifacts).
- **CAUSE:** `pgdata`/`sar-scene-artifacts` from an older run persist.
- **CHECK:** `docker volume ls`; `docker compose logs` for column-not-found.
- **FIX:** apply the migrations on the existing `pgdata`; only if you truly want a clean
  DB use the **destructive** `docker compose down -v` (Section 21) — never as routine.

### 20.15 Environment variables not loaded

- **SYMP:** services use surprising defaults; `POSTGRES_DB`/host look wrong.
- **CAUSE:** `.env` missing, renamed, or the compose project differs.
- **CHECK:** `grep POSTGRES_DB .env`; `docker compose exec data-ingestion printenv |
  grep POSTGRES`.
- **FIX:** create `.env` from `.env.example` and `docker compose up -d --force-recreate`.

---

## 21. Cleanup

**SAFE CLEANUP (keeps data):**

```bash
# Stop containers but KEEP the pgdata + sar-scene-artifacts + ml-models volumes
docker compose stop          # or: docker compose down   (down keeps volumes)
make down                    # docker compose down (keeps volumes)
make logs                    # tail logs
```

`docker compose down` (no `-v`) removes containers and the network but keeps all named
volumes. `docker compose stop postgres redis` stops only the infra.

Removing **temporary** SAR artifacts is not supported as a standalone safe one-liner
(rasters and `spill_candidates` rows must be removed together). Leave them unless you know
what you're doing.

**DESTRUCTIVE RESET (⚠️ only when intended):**

```bash
# Deletes Postgres data + all SAR artifacts + ML models. IRREVERSIBLE.
docker compose down -v --rmi local      # make clean
```

`docker compose down -v` wipes `pgdata` (all tables) and `sar-scene-artifacts` (all
rasters), so a fresh `pgdata` re-runs `init.sql`. This is the only repo-documented DB
"reset". Prefer re-applying the idempotent migrations over deleting the volume.

---

## 22. Security

- **Never commit GEE private keys.** `.gitignore` ignores `secrets/`; the key stays at
  host `./secrets/gee-key.json`, mounted read-only (`:ro`) to `/run/secrets/gee-key.json`.
  Never force-add or stage it.
- **Never commit `.env`.** It holds `POSTGRES_PASSWORD`, `AISSTREAM_API_KEY`,
  `VESSELAPI_API_KEY`, `GEE_SERVICE_ACCOUNT`; `.gitignore` ignores `.env`.
- **Do not expose credentials in logs.** The ingestion/reader code deliberately avoids
  logging keys/URLs — keep it that way.
- **Do not commit API keys** (AISStream/VesselAPI). The compose file includes placeholder
  AISStream defaults; override them only via `.env`.
- **Do not expose the service-account JSON** in git, tickets, screenshots, or this doc.
  Rotate immediately if leaked (delete the key in GCP IAM, generate a new one).

---

## 23. Known Limitations

The application is **not fully runnable out-of-the-box for the real SAR path** because of
external dependencies. Gaps:

- **Real SAR acquisition (Run A) needs Google Earth Engine credentials + Drive export
  permissions** (Section 6). Without them the SAR trigger logs an auth error and no real
  scene is processed.
- **Live AIS needs `AISSTREAM_API_KEY` / `VESSELAPI_API_KEY`**; without a key `ais-reader`
  idles. `POST /ingest/ais` is the substitute.
- **`SAR_MIN_AREA_M2` / `SAR_BRIGHT_TARGET_THRESHOLD` are not validated** — they must come
  from real validation data; the values shown in docs/tests are examples/fixtures.
- **Evidence-fusion windows** (`EVIDENCE_*`) are tunable assumptions, not validated.
- **`incident.fused` has no upstream consumer** that writes a `spill_incidents` row yet;
  the api-gateway relays it to the dashboard only.
- **The 4 skeleton services (8011–8014)** are heartbeat shells; their legacy tables are
  only filled by `scripts/seed_demo_data.py`.
- **`simulator` does not push data** into the pipeline (it prints and sleeps).
- **`secrets/gee-key.json` is required for the `data-ingestion` bind-mount** — if absent,
  compose fails to start that container even for AIS-only use. Provide the real key or a
  placeholder file before `docker compose up`.

Do **not** claim the full stack works for real oil-spill detection: the implemented,
verified path is the **synthetic demo** (Run B) and the **AIS/vessel pipeline**.