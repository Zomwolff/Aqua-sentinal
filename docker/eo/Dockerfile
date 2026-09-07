# ── eo container ─────────────────────────────────────────────────────────────
# The genuinely-trained model of the three "ML" containers. Wraps
# `optical imagery/sentinel2_interface.py` + the included checkpoint
# (sentinel2_cnn_10band_best.pth — a CNN trained on MADOS Sentinel-2 data,
# class 6 = "Oil Spill"). See docker/eo/app/main.py for the full design note:
# SAR is primary, this is secondary/non-mandatory verification, triggered per
# filtered SAR candidate, silently skipped when no cloud-free Sentinel-2
# scene exists for that location/time.
#
# Single process (unlike ais/sar/backend, nothing else shares this image) —
# no supervisord needed here.
FROM python:3.11-slim
WORKDIR /svc/eo

# rasterio needs GDAL at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev gcc g++ libexpat1 libcurl4 libpng16-16 libjpeg62-turbo libopenjp2-7 \
    && rm -rf /var/lib/apt/lists/*

COPY docker/eo/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Shared package — same minimal subset evidence-fusion uses (db.connection +
# redis_client + geo_utils/models/artifacts for consistency with the rest of
# the pipeline, even though eo doesn't touch all of them today).
COPY shared /svc/eo/shared
COPY services/shared/redis_client.py /svc/eo/shared/redis_client.py
COPY services/shared/geo_utils.py /svc/eo/shared/geo_utils.py
COPY services/shared/models.py /svc/eo/shared/models.py
COPY services/shared/artifacts.py /svc/eo/shared/artifacts.py

# The model itself: architecture + checkpoint, copied verbatim from
# `optical imagery/`. sentinel2_interface.py resolves its own checkpoint path
# relative to its own file location, so as long as these three files stay
# together this needs no path configuration.
COPY docker/eo/model /svc/eo/model

# The new service code (main.py, gee_optical.py) — everything here is NEW,
# nothing in `optical imagery/` itself was modified.
COPY docker/eo/app /svc/eo/app

EXPOSE 8016
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8016", "--log-level", "info"]
