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
# db.py) and shared.spatial; COPY order keeps the module, not the db/ package.
COPY shared ./shared
COPY services/shared ./shared

# Copy service code
COPY services/data-ingestion/app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
