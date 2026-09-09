FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev libgeos-c1v5 libexpat1 libcurl4 \
    && rm -rf /var/lib/apt/lists/*

COPY fusion/requirements-prod.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY fusion /app/fusion
COPY models /app/models

ENV PYTHONPATH=/app
ENTRYPOINT ["python", "-c", "from fusion import detect_oil; import numpy as np, sys; m=detect_oil(sys.argv[1], sys.argv[2]); print(m.shape, m.dtype, np.unique(m))"]
