# Aqua Sentinel — Maritime Oil-Spill Detection & Attribution

A real-time maritime intelligence system for the Smart India Hackathon that detects oil spills from SAR imagery and AIS vessel data, fuses the evidence, attributes spills to source vessels, and recommends response actions. A pipeline of Python/FastAPI microservices exchanges events over Redis Streams, persists spatio-temporal data in PostGIS, and serves a live Leaflet-based dashboard through an API gateway.

## Prerequisites

- Docker (with Docker Compose v2, i.e. `docker compose`, or the standalone `docker-compose`)
- A working `.env` file (see Setup)

## Setup

```bash
# 1. Clone or enter the project root
cd <project-root>

# 2. Create the .env file from the template and edit values if needed
cp .env.example .env

# 3. (Optional) Validate the compose file before building
docker-compose config

# 4. Build and start the full stack (first build pulls base images, takes a few minutes)
docker-compose up --build

# 5. Verify everything is healthy
docker-compose ps
docker-compose logs -f            # watch all service logs

# 6. Check service health endpoints (example: api-gateway)
curl http://localhost:8015/health

# 7. Stop the stack
docker-compose down               # stop containers (named volume `pgdata` is kept)
docker-compose down -v            # stop AND delete the Postgres data volume
```

Open the dashboard at `http://localhost:3000`. Health checks are available at `http://localhost:<port>/health` for every service.

> **Port note:** host ports 5433 and 6380 are used instead of the default 5432/6379 so the stack can coexist with native PostgreSQL/Redis services already running on this machine. If you don't have local services on 5432/6379, you can set `ports` back to `5432:5432` and `6379:6379` in `docker-compose.yml`.

| Port | Service |
|------|---------|
| 3000 | Dashboard (React/Vite, served by nginx) |
| 5433 | PostgreSQL 15 + PostGIS (mapped from container 5432) |
| 6380 | Redis 7 (mapped from container 6379) |

## Database

PostgreSQL 15 + PostGIS is the central persistent store. The schema is defined in `infra/postgres/init.sql` and is initialized automatically on the first start of a fresh `pgdata` volume (9 tables, PostGIS `GEOMETRY(...,4326)` columns, GIST/b-tree indexes).

Connection details come from the `.env` file (`POSTGRES_*` variables). Inside the Docker network the hostname is `postgres` on port `5432`; from your machine it's `localhost:5433`.

```bash
# Inspect via psql inside the container
docker compose exec postgres psql -U aqua_sentinel -d aqua_sentinel

# Verify the schema (9 tables, columns, PKs, FKs, spatial types, indexes)
python scripts/verify_db.py              # needs: pip install -r scripts/requirements.txt

# Optional demo data (a few vessels, positions, one spill, attribution, forecasts,
# severity, recommendations, protected area, environmental observations)
python scripts/seed_demo_data.py
```

> **Spatial rule:** columns are stored as `GEOMETRY(...,4326)` (degrees). For real-world distances use a geography cast — e.g. `ST_Distance(a.geom::geography, b.geom::geography)` returns meters, `ST_DWithin(a.geom::geography, b.geom::geography, radius_meters)`. Topology ops (`ST_Intersects`, `ST_Contains`, `ST_Within`) stay geometry-based. See `docs/spatial.md` and the `shared/spatial` helpers.

## Tests

```bash
docker run --rm --network aqua-sentinal_aqua-net \
  -e POSTGRES_HOST=postgres -e POSTGRES_DB=aqua_sentinel \
  -e POSTGRES_USER=aqua_sentinel -e POSTGRES_PASSWORD=change_me \
  -v "$PWD":/work -w /work python:3.11-slim \
  sh -c "pip install -q -r tests/requirements.txt && python -m pytest tests/ -v"
```

Spatial regression tests verify meter-vs-degree distance, `ST_DWithin` radius semantics, geometry-based topology, and longitude/latitude order.

## Service ports

Each service runs uvicorn on port 8000 inside its container and is exposed on a unique host port:

| Port | Service | Purpose |
|------|---------|---------|
| 8001 | data-ingestion | Ingests AIS/SAR feeds and publishes raw events to Redis Streams |
| 8002 | ais-analytics | Computes per-vessel behavioral features (speed/course statistics) |
| 8003 | anomaly-detection | Flags AIS behavioral anomalies (stoppages, deviations, loitering) |
| 8004 | dark-vessel-detection | Detects dark (non-transmitting) vessels from SAR imagery |
| 8005 | ais-spoof-detection | Detects spoofed or inconsistent AIS signals |
| 8006 | sts-detection | Detects ship-to-ship (STS) transfer events |
| 8007 | vessel-risk-engine | Ranks vessels by risk from features and history |
| 8008 | sar-spill-intelligence | Extracts oil-spill candidates from SAR tiles |
| 8009 | lookalike-engine | Retrieves historical lookalike spill events |
| 8010 | evidence-fusion | Fuses SAR, AIS, and environmental evidence into incidents |
| 8011 | source-attribution | Attributes incidents to likely source vessels |
| 8012 | drift-forecast | Forecasts spill drift under met-ocean forcing |
| 8013 | severity-impact | Assesses severity and environmental/coastal impact |
| 8014 | response-decision | Recommends priority response actions |
| 8015 | api-gateway | Public REST API + live WebSocket relay to the dashboard |

## SAR oil-spill pipeline — B1 segmentation + B2 look-alike classifier

Deep-learning SAR spill detection lives in `sar-LinkNet-ResNet34/` (LinkNet + ResNet34, **separate from** the `sar-spill-intelligence` CFAR microservice on port 8008):

```powershell
cd sar-LinkNet-ResNet34
.\.venv\Scripts\python.exe infer_pipeline.py --checkpoint checkpoints/best_model.pth --input path\to\scene.tif --output-dir outputs/demo --rescale
.\.venv\Scripts\python.exe geo_postprocess.py --mask outputs/demo/scene_mask.png --source-image path\to\scene.tif --output-dir outputs/demo --glcm-band 1
.\.venv\Scripts\python.exe b2_lookalike.py --input outputs/demo/scene_spill_meta.json --output outputs/b2_predictions.json
```

That chain produces the mask → GIS polygons + 14 per-candidate B2 features (GLCM/shape/edge/context) → OIL/LOOK_ALIKE predictions from `models/b2_random_forest.joblib`. Batch training data via `at.py`, classifier training via `train_b2.py`. **Full teammate guide (setup, flags, outputs, troubleshooting, honest model limitations): [`sar-LinkNet-ResNet34/README.md`](sar-LinkNet-ResNet34/README.md).**

## Project structure

```
.
├── docker-compose.yml        # Full stack orchestration
├── .env.example              # Environment variable template
├── infra/                    # postgres init schema + redis config
│   ├── postgres/init.sql     # PostGIS extension, tables, indexes
│   └── redis/redis.conf
├── data/                     # Mounted (read-only) into the simulator
│   ├── sample_sar/           # SAR imagery samples
│   ├── sample_ais/           # AIS message samples
│   ├── env_layers/           # Environmental grids (wind, currents)
│   └── geo_layers/           # Coastlines, EEZ, protected areas
├── services/                 # 15 FastAPI microservices, one container each
│   └── <service>/            # Dockerfile, requirements.txt, app/main.py
├── sar-LinkNet-ResNet34/     # SAR DL pipeline: B1 inference, GIS postprocess,
│                             # B2 features/training/inference (own README + venv)
├── dashboard/                # React + Vite + Leaflet frontend
├── simulator/                # Data replay simulator (script, not a server)
├── shared/                   # Shared Python modules (mounted into all services)
│   ├── db/                   # asyncpg pool (connection.py) + pydantic models
│   └── spatial/              # geography-cast distance helpers + constants
├── scripts/                  # verify_db.py, seed_demo_data.py
├── tests/                    # pytest regression tests (spatial)
└── docs/                     # architecture.md, api-contracts.md, spatial.md
```
