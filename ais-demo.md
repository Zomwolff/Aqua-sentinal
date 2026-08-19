# Aqua-Sentinel Demo & Testing Guide

Currently, the Aqua-Sentinel pipeline operates completely headlessly in the background. It silently processes thousands of data points across Redis, PostgreSQL, and background Python workers.

Because of this architecture, **"all you can see is data being fetched"** in the terminal, while the actual intelligence generation is hidden deep within the database and message streams.

This guide provides full transparency. It will walk you through exactly what happens after the data is fetched, how to test each feature, and how to "look under the hood" to see the intelligence engine working in real-time.

---

## 1. The Ingestion Layer (Is data flowing?)

When the system boots, `data-ingestion` connects to AISStream.io and starts receiving raw ping data.

**Test it:** 
You can check the real-time health and deduplication stats by pinging the internal FastAPI endpoint.
```bash
curl http://localhost:8000/ingest/status
```
*What this shows:* How many messages have been processed, rejected (due to bad data or duplication), and the exact time of the last heartbeat.

---

## 2. The Contextual Layer (Weather Integration)

Every 15 minutes, the system reaches out to the Open-Meteo Marine API to get the wind speed and ocean currents for the Mumbai offshore bounding box.

**Test it:**
Run this command to look inside the PostgreSQL database and see the weather data the system is using to suppress false positives:
```bash
docker exec aqua-sentinal-postgres-1 psql -U postgres -d maritime_oilspill -c "
SELECT timestamp, wind_speed_kmh, wind_direction_deg, current_speed_ms 
FROM environmental_conditions 
ORDER BY timestamp DESC LIMIT 5;
"
```
*What this shows:* If `wind_speed_kmh` > 50, the anomaly engine knows there is a storm and will stop flagging ships for "erratic courses" or "sudden stops".

---

## 3. The Analytics Layer (15-Minute Windows)

Raw AIS data is noisy. The `ais-analytics` service groups 15 minutes worth of pings for a single ship and calculates a "Behavioral Feature Vector" (e.g., speed variance, loitering score, max rate of turn).

**Test it:**
Because it takes 15 minutes of data collection to generate one vector, you won't see anything immediately upon booting. Wait 15-20 minutes, then run:
```bash
docker exec aqua-sentinal-postgres-1 psql -U postgres -d maritime_oilspill -c "
SELECT mmsi, window_start, avg_speed, loitering_score, max_rate_of_turn_deg_min 
FROM vessel_features 
ORDER BY window_start DESC LIMIT 10;
"
```
*What this shows:* The mathematical breakdown of how ships are behaving over time. These rows are what the ML model uses to learn.

---

## 4. The Intelligence Layer (Anomalies & Rules)

The `anomaly-detection` service reads the feature vectors and applies the heuristic rules (e.g., speed drops, zig-zags) and the ML Isolation Forest. 

**Test it (Live Logs):**
To see the brain actually working and catching anomalies in real time, tail the logs of the detection worker:
```bash
docker logs -f aqua-sentinal-anomaly-detection-1
```
*What this shows:* You will see logs like `[INFO] Anomaly: MMSI=419001801 type=ais_gap severity=MEDIUM`.

**Test it (Database Storage):**
To see the high-confidence anomalies that were saved permanently:
```bash
docker exec aqua-sentinal-postgres-1 psql -U postgres -d maritime_oilspill -c "
SELECT mmsi, anomaly_type, severity, source, confidence_score 
FROM anomaly_events 
ORDER BY window_start DESC LIMIT 10;
"
```
*What this shows:* A list of flagged vessels. The `source` column will tell you if the anomaly was caught by a static rule (`rules`) or the Machine Learning model (`isolation_forest`).

---

## 5. Machine Learning (Isolation Forest)

The ML model needs at least 100 15-minute feature vectors to learn what "normal" looks like. It trains silently in the background every 1 hour. 

**Test it (Manual Training):**
If you don't want to wait an hour, you can manually trigger the AI to look at the database and train itself immediately by running this command:
```bash
docker exec aqua-sentinal-anomaly-detection-1 python -c "
import asyncio
from app.ml_model import train_isolation_forest
asyncio.run(train_isolation_forest())
"
```
*What this shows:* If you have > 100 samples in the database, the model will compile, save to disk, and immediately start predicting complex anomalies on live data. If you have < 100, it will gracefully skip training.

---

## Summary of the Data Flow
For full transparency, here is the exact life cycle of a single ship coordinate in your system:
1. `ais-reader` fetches coordinate -> Redis stream `ais.raw`
2. `data-ingestion` cleans/deduplicates coordinate -> PostgreSQL `vessel_positions` AND Redis stream `ais.clean`
3. (After 15 minutes) `ais-analytics` bundles the coordinates -> PostgreSQL `vessel_features` AND Redis stream `ais.features`
4. `anomaly-detection` reads the feature bundle + weather data -> Flags anomaly -> PostgreSQL `anomaly_events`.
