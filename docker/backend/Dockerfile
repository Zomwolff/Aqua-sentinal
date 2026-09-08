# ── backend container ────────────────────────────────────────────────────────
# Consolidates the glue/decision/forecast layer, none of which is a model:
# api-gateway (the public REST/WebSocket entrypoint the frontend talks to),
# evidence-fusion (weighted-sum combiner over SAR+AIS+environmental+optical
# evidence), source-attribution (backward search over AIS tracks near an
# estimated spill origin), severity-impact and response-decision (geospatial
# heuristics + lookup tables), drift-forecast (physics-based particle
# advection — ITOPF 3%-wind-drift rule run forward/backward; NOT a trained
# model, moved here rather than into the `eo` container so `eo` can mean
# exactly one thing: the optical satellite CNN), and ecological-impact
# (spatial exposure calculation — PostGIS intersection of forecast polygons
# with ecological receptors).
#
# Each original service keeps its own untouched app/ + shared/ tree under
# /svc/<name>, copied exactly as its original Dockerfile did — no source
# edits (except evidence-fusion/app/{evidence.py,worker.py}, which gained a
# few additive lines to read/forward optical evidence from the eo service —
# see docker/EO_CORRECTION.md). supervisord runs all seven, each still bound
# to its ORIGINAL port. api-gateway (:8015) is the only one published to the
# host.
FROM python:3.11-slim
WORKDIR /svc

COPY docker/backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── evidence-fusion :8010 ─────────────────────────────────────────────────────
RUN mkdir -p /svc/evidence-fusion
COPY shared /svc/evidence-fusion/shared
COPY services/shared/redis_client.py /svc/evidence-fusion/shared/redis_client.py
COPY services/shared/geo_utils.py /svc/evidence-fusion/shared/geo_utils.py
COPY services/shared/models.py /svc/evidence-fusion/shared/models.py
COPY services/shared/artifacts.py /svc/evidence-fusion/shared/artifacts.py
COPY services/evidence-fusion/app /svc/evidence-fusion/app
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/evidence-fusion")#' /svc/evidence-fusion/app/*.py

# ── source-attribution :8011 ──────────────────────────────────────────────────
RUN mkdir -p /svc/source-attribution
COPY shared/db /svc/source-attribution/shared/db
COPY shared/spatial /svc/source-attribution/shared/spatial
COPY shared/__init__.py /svc/source-attribution/shared/__init__.py
COPY services/shared/redis_client.py /svc/source-attribution/shared/redis_client.py
COPY services/source-attribution/app /svc/source-attribution/app
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/source-attribution")#' /svc/source-attribution/app/*.py

# ── severity-impact :8013 ─────────────────────────────────────────────────────
RUN mkdir -p /svc/severity-impact
COPY shared/db /svc/severity-impact/shared/db
COPY shared/spatial /svc/severity-impact/shared/spatial
COPY shared/__init__.py /svc/severity-impact/shared/__init__.py
COPY services/shared/redis_client.py /svc/severity-impact/shared/redis_client.py
COPY services/severity-impact/app /svc/severity-impact/app
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/severity-impact")#' /svc/severity-impact/app/*.py

# ── response-decision :8014 ───────────────────────────────────────────────────
RUN mkdir -p /svc/response-decision
COPY services/shared /svc/response-decision/shared
COPY services/response-decision/app /svc/response-decision/app
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/response-decision")#' /svc/response-decision/app/*.py

# ── drift-forecast :8012 (particle advection physics) ─────────────────────────
RUN mkdir -p /svc/drift-forecast
COPY shared/db /svc/drift-forecast/shared/db
COPY shared/spatial /svc/drift-forecast/shared/spatial
COPY shared/__init__.py /svc/drift-forecast/shared/__init__.py
COPY services/shared/redis_client.py /svc/drift-forecast/shared/redis_client.py
COPY services/drift-forecast/app /svc/drift-forecast/app
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/drift-forecast")#' /svc/drift-forecast/app/*.py

# ── ecological-impact :8016 (spatial exposure calculation) ────────────────────
RUN mkdir -p /svc/ecological-impact
COPY shared/db /svc/ecological-impact/shared/db
COPY shared/__init__.py /svc/ecological-impact/shared/__init__.py
COPY services/shared/redis_client.py /svc/ecological-impact/shared/redis_client.py
COPY services/ecological-impact/app /svc/ecological-impact/app
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/ecological-impact")#' /svc/ecological-impact/app/*.py

# ── weather-ingestion (environmental forecast data polling) ───────────────────
RUN mkdir -p /svc/weather-ingestion/app
COPY shared/db /svc/weather-ingestion/shared/db
COPY shared/__init__.py /svc/weather-ingestion/shared/__init__.py
COPY services/data-ingestion/app/weather.py /svc/weather-ingestion/app/weather.py
RUN echo 'import asyncio\nimport sys\nimport logging\nlogging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")\nsys.path.insert(0, "/svc/weather-ingestion")\nfrom shared.db.connection import create_pool, close_pool\nfrom app.weather import weather_polling_loop\n\nasync def main():\n    await create_pool()\n    try:\n        await weather_polling_loop()\n    finally:\n        await close_pool()\n\nif __name__ == "__main__":\n    asyncio.run(main())' > /svc/weather-ingestion/app/worker.py

# ── api-gateway :8015 (public entrypoint) ─────────────────────────────────────
RUN mkdir -p /svc/api-gateway
COPY services/shared /svc/api-gateway/shared
COPY AIS/services/api-gateway/app /svc/api-gateway/app
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/api-gateway")#' /svc/api-gateway/app/*.py

COPY docker/backend/supervisord.conf /etc/supervisor/supervisord.conf

EXPOSE 8015
CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
