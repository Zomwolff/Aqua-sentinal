FROM python:3.11-slim
WORKDIR /app

# Rasterio/pyproj wheels bundle their geospatial runtime libraries.

COPY fusion/requirements-prod.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*


COPY fusion /app/fusion
COPY models /app/models

ENV PYTHONPATH=/app
CMD ["uvicorn", "fusion.app:app", "--host", "0.0.0.0", "--port", "8017"]
