FROM python:3.11-slim
WORKDIR /app
COPY services/response-decision/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/db ./shared/db
COPY shared/__init__.py ./shared/__init__.py
COPY services/shared/redis_client.py ./shared/redis_client.py
COPY services/shared/geo_utils.py ./shared/geo_utils.py
COPY services/response-decision/app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]