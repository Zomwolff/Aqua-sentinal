FROM python:3.11-slim

WORKDIR /app

# Copy and install dependencies
COPY AIS/services/spill-simulator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared modules from services/shared (needed for DB access)
COPY services/shared /app/shared

# Copy simulation scenarios
COPY AIS/simulation_scenarios /app/simulation_scenarios

# Copy spill-simulator application
COPY AIS/services/spill-simulator/app /app/app

# Run the simulator
CMD ["python", "-m", "app.main"]
