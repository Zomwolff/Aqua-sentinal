# ── sar container ────────────────────────────────────────────────────────────
# Consolidates: data-ingestion (Earth-Engine SAR scene acquisition — fetches
# Sentinel-1 GeoTIFFs, publishes `sar.clean`; its AIS-batch-ingest HTTP path
# is unused in this deployment, kept dormant), sar-spill-intelligence (Lee
# despeckle -> CFAR -> morphology -> polygonize — classical signal
# processing, not a trained model), and lookalike-engine (oil vs.
# biogenic-slick vs. low-wind classifier — engineered features, not deep
# learning).
#
# NOTE: this repo's `sar/` folder was renamed to `sar-cfar/` (to make room
# for the new `sar-LinkNet-ResNet34/` deep-learning folder alongside it) —
# every COPY path below reflects that rename. Per your call, the LinkNet
# model is NOT wired in yet (no trained checkpoint is committed — see
# sar-LinkNet-ResNet34/README.md, train via oil_spill_training.ipynb first).
# CFAR is the only detector running in this container for now.
#
# Each original service keeps its own untouched app/ + shared/ tree under
# /svc/<name>, copied exactly as its original Dockerfile did — no source
# edits. supervisord runs all three, each still bound to its ORIGINAL port.
FROM python:3.11-slim
WORKDIR /svc

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev gcc g++ \
    libexpat1 libcurl4 libpng16-16 libjpeg62-turbo libopenjp2-7 libgeos-c1v5 \
    && rm -rf /var/lib/apt/lists/*

COPY docker/sar/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── data-ingestion :8001 (SAR/GEE acquisition) ───────────────────────────────
RUN mkdir -p /svc/data-ingestion
COPY shared/spatial /svc/data-ingestion/shared/spatial
COPY shared/__init__.py /svc/data-ingestion/shared/__init__.py
COPY services/shared/db.py /svc/data-ingestion/shared/db.py
COPY services/shared/redis_client.py /svc/data-ingestion/shared/redis_client.py
COPY services/shared/geo_utils.py /svc/data-ingestion/shared/geo_utils.py
COPY services/shared/models.py /svc/data-ingestion/shared/models.py
COPY services/shared/artifacts.py /svc/data-ingestion/shared/artifacts.py
COPY services/data-ingestion/app /svc/data-ingestion/app
COPY sar-cfar/acquisition/sar_acquisition.py /svc/data-ingestion/app/sar_acquisition.py
COPY sar-cfar/acquisition/synthetic_injection.py /svc/data-ingestion/app/synthetic_injection.py
COPY sar-cfar/acquisition/dynamic_sar_worker.py /svc/data-ingestion/app/dynamic_sar_worker.py
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/data-ingestion")#' /svc/data-ingestion/app/*.py

# ── sar-spill-intelligence :8008 (CFAR pipeline) ─────────────────────────────
RUN mkdir -p /svc/sar-spill-intelligence
COPY shared /svc/sar-spill-intelligence/shared
COPY services/shared/redis_client.py /svc/sar-spill-intelligence/shared/redis_client.py
COPY services/shared/geo_utils.py /svc/sar-spill-intelligence/shared/geo_utils.py
COPY services/shared/models.py /svc/sar-spill-intelligence/shared/models.py
COPY services/shared/artifacts.py /svc/sar-spill-intelligence/shared/artifacts.py
COPY sar-cfar/sar-spill-intelligence/app /svc/sar-spill-intelligence/app
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/sar-spill-intelligence")#' /svc/sar-spill-intelligence/app/*.py

# ── lookalike-engine :8009 (oil vs. lookalike classifier) ────────────────────
RUN mkdir -p /svc/lookalike-engine
COPY shared /svc/lookalike-engine/shared
COPY services/shared/redis_client.py /svc/lookalike-engine/shared/redis_client.py
COPY services/shared/geo_utils.py /svc/lookalike-engine/shared/geo_utils.py
COPY services/shared/models.py /svc/lookalike-engine/shared/models.py
COPY services/shared/artifacts.py /svc/lookalike-engine/shared/artifacts.py
COPY services/lookalike-engine/app /svc/lookalike-engine/app
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/lookalike-engine")#' /svc/lookalike-engine/app/*.py

COPY docker/sar/supervisord.conf /etc/supervisor/supervisord.conf

CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
