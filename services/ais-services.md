Step 1: Start Docker Desktop


Step 2: Start the Backend Services
Once Docker is running, you can start the entire Phase-1 pipeline 
Open your terminal, run- 

make up-ais

(This will download the necessary base images, build your custom python containers, and start them in the background).

Step 3: Check if everything is healthy
You can verify that all services started correctly and are talking to each other by running:

bash


make health

This will hit the /health endpoint of every microservice and report if they are alive and processing data.


Here's a full summary of every endpoint that now exists in the API gateway at http://localhost:8015:

✅ API Gateway — All Endpoints at localhost:8015


📋 System / Monitoring
Endpoint	What it shows
GET /health	Gateway liveness
GET /system/health	Health of ALL 6 microservices simultaneously
GET /system/pipeline	Redis stream lengths + consumer group lag (are services keeping up?)
GET /system/stats	Counts: tracked vessels, positions/hr, anomalies/24h, risk tiers, etc.


🚢 Vessels
Endpoint	What it shows
GET /vessels	All tracked vessels (filter by type, risk_tier, active window)
GET /vessels/{mmsi}	Full detail: position + features + risk + anomalies + trust
GET /vessels/{mmsi}/track	Position track (last N hours) for drawing map paths
GET /vessels/{mmsi}/anomalies	Anomaly history for one vessel
GET /vessels/{mmsi}/trust	Trust score history for one vessel
GET /vessels/{mmsi}/risk	Risk score + factor breakdown for one vessel
GET /vessels/risk/leaderboard	Top N highest-risk vessels


⚠️ Anomalies / STS / Spoofing
Endpoint	What it shows
GET /anomalies	All anomalies (filter: severity, type, mmsi, since_hours)
GET /sts	Completed STS encounters with vessel names + coordinates
GET /sts/active	Live ongoing encounters (direct from Redis)
GET /spoofing/suspects	Vessels below trust threshold
GET /features	Raw 15-min behavioral feature windows


🎯 Risk
Endpoint	What it shows
GET /risk/vessels	All risk-scored vessels (filter by tier/score)
GET /risk/tasking-requests	Satellite tasking requests for HIGH/CRITICAL


⚡ Real-time
Endpoint	What it shows
WS /live	WebSocket: pushes anomalies, risk changes, STS events in real time