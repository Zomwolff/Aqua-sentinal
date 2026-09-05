FROM python:3.11-slim
WORKDIR /app
COPY AIS/services/ais-spoof-detection/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY services/shared ./shared
COPY AIS/services/ais-spoof-detection/app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
