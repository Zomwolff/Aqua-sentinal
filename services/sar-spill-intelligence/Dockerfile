FROM python:3.11-slim
WORKDIR /app

# rasterio wheels need GDAL runtime libraries that the slim image omits
# (libexpat, curl, png/jpeg, openjpeg). shapely wheels need the GEOS C library.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libexpat1 libcurl4 libpng16-16 libjpeg62-turbo libopenjp2-7 libgeos-c1v5 \
    && rm -rf /var/lib/apt/lists/*

COPY services/sar-spill-intelligence/requirements.txt ./
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

COPY services/sar-spill-intelligence/app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]