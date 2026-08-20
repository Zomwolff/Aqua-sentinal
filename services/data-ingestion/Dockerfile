FROM python:3.11-slim

WORKDIR /app

# System deps for geopandas / shapely / rasterio
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev gcc g++ \
    libexpat1 libcurl4 libpng16-16 libjpeg62-turbo libopenjp2-7 libgeos-c1v5 && \
    rm -rf /var/lib/apt/lists/*

COPY services/data-ingestion/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Shared library: canonical top-level shared/ (spatial) plus the AIS-pipeline
# shared modules. data-ingestion uses shared.db as a MODULE (services/shared/
# db.py) and shared.spatial; copy selectively to avoid package/module collision.
COPY shared/spatial ./shared/spatial
COPY shared/__init__.py ./shared/__init__.py
COPY services/shared/db.py ./shared/db.py
COPY services/shared/redis_client.py ./shared/redis_client.py
COPY services/shared/geo_utils.py ./shared/geo_utils.py
COPY services/shared/models.py ./shared/models.py
COPY services/shared/artifacts.py ./shared/artifacts.py

# Copy service code
COPY services/data-ingestion/app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
