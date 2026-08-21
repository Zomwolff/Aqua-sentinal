# Authority Command Dashboard

The Authority Command Dashboard is a separate React/MapLibre operational service for coastal and regulatory authorities. It does not replace the existing dashboard and is served on **http://localhost:3001** (`authority-command-dashboard` in Compose).

## Architecture and configuration

The browser uses same-origin `/api` and `/live`; its nginx container proxies those paths over `aqua-net` to `api-gateway:8000`. This avoids browser-to-container `localhost` errors. In development, `npm run dev` uses port 3001 and proxies to the host Gateway at 8015. `VITE_API_BASE_URL` is a future deployment configuration point; it defaults to `/api`.

The Gateway permits `http://localhost:3000,http://localhost:3001` by default through `ALLOWED_ORIGINS`. Production deployments should set this variable to their exact trusted UI origins.

## API dependencies

The service intentionally consumes the Gateway aggregation contract only:

- `GET /spill/incidents` for the alert feed.
- `GET /spill/incidents/{id}` for the full incident, including real spill GeoJSON, severity, attribution, forecast polygons, and Response Decision Engine recommendations.
- `GET /vessels`, `GET /vessels/{mmsi}` for the AIS layer and vessel detail.
- `GET /dark-vessels` for SAR/AIS dark-vessel detections.
- `GET /protected-areas` for persisted marine protected/reference zones.
- `PATCH /spill/recommendations/{id}/acknowledge` to acknowledge an existing `response_recommendations` record through the Gateway.
- `WS /live` for small Redis-derived event envelopes. The client refetches REST resources after relevant events and reconnects with capped exponential backoff.

The current schema/service has acknowledgement but no assignment field or endpoint. `ASSIGN UNAVAILABLE` is therefore deliberately disabled rather than pretending to persist an assignment.

## Data and map layers

It reads PostGIS-backed `spill_incidents`, `forecasts`, `severity`, `attribution_results`, `response_recommendations`, `vessels`/`vessel_risk_scores`, `dark_vessel_events`, and `protected_areas` through the Gateway. The right-side operational map renders actual GeoJSON spill polygons, selected forecast geometry, normal AIS vessels, dark-vessel detections, and protected/reference zones. Forecast selection is client-side over the already-loaded incident forecast array; unavailable 6/12/24/48/72-hour buttons remain disabled.

## Run and troubleshoot

```bash
docker compose up --build authority-command-dashboard api-gateway
# open http://localhost:3001
```

If the UI reports a disconnected backend, first check `curl http://localhost:8015/health` and `docker compose logs api-gateway authority-command-dashboard`. An empty incident feed, missing forecast, or missing recommendations is a real no-data state, not demo data; use the project’s explicit `scripts/seed_demo_data.py` only when a local database needs demonstration data.

For a non-destructive local Authority Dashboard demonstration, use `python3 scripts/seed_authority_dashboard_demo.py` with `POSTGRES_HOST=localhost POSTGRES_PORT=5433 POSTGRES_USER=postgres POSTGRES_PASSWORD=postgres POSTGRES_DB=maritime_oilspill REDIS_HOST=localhost REDIS_PORT=6380`. It appends one explicitly labelled synthetic incident and publishes the normal severity event, allowing the existing Response Decision Engine to generate its own recommendations. It does not truncate operational tables.
