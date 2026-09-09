# ── ais container ────────────────────────────────────────────────────────────
# Consolidates: ais-reader, ais-analytics, anomaly-detection, ais-spoof-detection,
# sts-detection, dark-vessel-detection, vessel-risk-engine.
#
# Honest framing: this is the "AIS ML" container in name only for one of its
# seven processes (anomaly-detection periodically retrains a small
# scikit-learn model into ML_MODEL_DIR). The rest — analytics, spoof/STS/dark-
# vessel detection, risk scoring — are explainable rule/threshold engines, not
# trained models.
#
# Each original service keeps its own untouched app/ + shared/ tree under
# /svc/<name>, exactly as its original Dockerfile copied it — no source
# edits. supervisord runs all seven as separate processes in one container,
# each still bound to its ORIGINAL port.
FROM python:3.11-slim
WORKDIR /svc

COPY docker/ais/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── ais-reader (plain script, no HTTP port) ──────────────────────────────────
RUN mkdir -p /svc/ais-reader
COPY AIS/services/ais-reader/app /svc/ais-reader/app
COPY services/shared /svc/ais-reader/shared
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/ais-reader")#' /svc/ais-reader/app/*.py

# ── ais-analytics :8002 ───────────────────────────────────────────────────────
RUN mkdir -p /svc/ais-analytics
COPY AIS/services/ais-analytics/app /svc/ais-analytics/app
COPY services/shared /svc/ais-analytics/shared
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/ais-analytics")#' /svc/ais-analytics/app/*.py

# ── anomaly-detection :8003 ───────────────────────────────────────────────────
RUN mkdir -p /svc/anomaly-detection
COPY AIS/services/anomaly-detection/app /svc/anomaly-detection/app
COPY AIS/services/calibration /svc/anomaly-detection/calibration
COPY services/shared /svc/anomaly-detection/shared
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/anomaly-detection")#' /svc/anomaly-detection/app/*.py

# ── dark-vessel-detection :8004 ───────────────────────────────────────────────
RUN mkdir -p /svc/dark-vessel-detection
COPY AIS/services/dark-vessel-detection/app /svc/dark-vessel-detection/app
COPY services/shared /svc/dark-vessel-detection/shared
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/dark-vessel-detection")#' /svc/dark-vessel-detection/app/*.py

# ── ais-spoof-detection :8005 ─────────────────────────────────────────────────
RUN mkdir -p /svc/ais-spoof-detection
COPY AIS/services/ais-spoof-detection/app /svc/ais-spoof-detection/app
COPY services/shared /svc/ais-spoof-detection/shared
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/ais-spoof-detection")#' /svc/ais-spoof-detection/app/*.py

# ── sts-detection :8006 ───────────────────────────────────────────────────────
RUN mkdir -p /svc/sts-detection
COPY AIS/services/sts-detection/app /svc/sts-detection/app
COPY services/shared /svc/sts-detection/shared
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/sts-detection")#' /svc/sts-detection/app/*.py

# ── vessel-risk-engine :8007 ──────────────────────────────────────────────────
RUN mkdir -p /svc/vessel-risk-engine
COPY AIS/services/vessel-risk-engine/app /svc/vessel-risk-engine/app
COPY services/shared /svc/vessel-risk-engine/shared
RUN sed -i 's#sys\.path\.insert(0, "/app")#sys.path.insert(0, "/svc/vessel-risk-engine")#' /svc/vessel-risk-engine/app/*.py

COPY docker/ais/supervisord.conf /etc/supervisor/supervisord.conf

CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
