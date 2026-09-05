FROM python:3.11-slim
WORKDIR /app
COPY AIS/services/ais-reader/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY services/shared ./shared
COPY AIS/services/ais-reader/app ./app
# Plain python - no uvicorn, no HTTP server
CMD ["python", "-u", "app/reader.py"]
