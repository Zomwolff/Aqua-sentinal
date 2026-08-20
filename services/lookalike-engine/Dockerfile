FROM python:3.11-slim
WORKDIR /app
COPY services/lookalike-engine/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Canonical top-level shared package (shared/spatial, shared/db) plus the
# AIS-pipeline shared modules (redis_client, geo_utils, models).
# services/shared/db.py is intentionally NOT copied: it would collide with the
# canonical shared/db/ package namespace served by the top-level shared/ tree.
COPY shared ./shared
COPY services/shared/redis_client.py ./shared/redis_client.py
COPY services/shared/geo_utils.py ./shared/geo_utils.py
COPY services/shared/models.py ./shared/models.py
COPY services/shared/artifacts.py ./shared/artifacts.py

COPY services/lookalike-engine/app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]