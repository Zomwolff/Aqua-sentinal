FROM python:3.11-slim
WORKDIR /app
COPY services/dark-vessel-detection/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY services/shared ./shared
COPY services/dark-vessel-detection/app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]