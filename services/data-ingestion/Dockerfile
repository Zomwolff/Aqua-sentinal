FROM python:3.11-slim

WORKDIR /app

# System deps for geopandas / shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY services/data-ingestion/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared library (accessible because build context = repo root)
COPY services/shared ./shared

# Copy service code
COPY services/data-ingestion/app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
