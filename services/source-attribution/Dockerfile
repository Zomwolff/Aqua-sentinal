FROM python:3.11-slim
WORKDIR /app
COPY services/source-attribution/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/db ./shared/db
COPY shared/spatial ./shared/spatial
COPY shared/__init__.py ./shared/__init__.py
COPY services/shared/redis_client.py ./shared/redis_client.py
COPY services/source-attribution/app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
