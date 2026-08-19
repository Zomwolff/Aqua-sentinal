# Aqua-Sentinel — convenience Makefile
# Usage: make <target>

.PHONY: help up up-ais down logs ps build rebuild health clean reader reader-setup

# ── Phase-1 AIS pipeline services ──────────────────────────────────────────────
AIS_SERVICES := postgres redis data-ingestion ais-analytics anomaly-detection \
                ais-spoof-detection sts-detection vessel-risk-engine api-gateway ais-reader

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*##"}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ── Core operations ────────────────────────────────────────────────────────────

up: ## Start ALL services (full stack)
	docker compose up -d

up-ais: ## Start Phase-1 AIS pipeline only (fast)
	docker compose up -d $(AIS_SERVICES)

down: ## Stop and remove all containers
	docker compose down

## ── AIS Native Reader (runs on host, bypasses Docker NAT) ──────────────────────

reader-setup: ## Install Python deps for the native AIS reader
	pip3 install asyncpg "redis[hiredis]" websockets pydantic

reader: ## Run the AIS reader natively on this machine (bypasses Docker NAT)
	@echo "Starting AIS reader on host — Ctrl+C to stop"
	python3 run_ais_reader.py

restart: ## Restart all running services
	docker compose restart

logs: ## Tail logs for all services (ctrl+c to stop)
	docker compose logs -f --tail=50

logs-ais: ## Tail logs for AIS pipeline services only
	docker compose logs -f --tail=50 $(AIS_SERVICES)

ps: ## Show container status
	docker compose ps

# ── Build ──────────────────────────────────────────────────────────────────────

build: ## Build all images
	docker compose build

build-ais: ## Build Phase-1 AIS pipeline images only
	docker compose build $(AIS_SERVICES)

rebuild: ## Force-rebuild all images (no cache)
	docker compose build --no-cache

rebuild-ais: ## Force-rebuild Phase-1 AIS images
	docker compose build --no-cache $(AIS_SERVICES)

# ── Health checks ──────────────────────────────────────────────────────────────

health: ## Check health endpoints for all Phase-1 services
	@echo "=== data-ingestion (8001) ==="
	@curl -sf http://localhost:8001/health | python3 -m json.tool 2>/dev/null || echo "DOWN"
	@echo ""
	@echo "=== ais-analytics (8002) ==="
	@curl -sf http://localhost:8002/health | python3 -m json.tool 2>/dev/null || echo "DOWN"
	@echo ""
	@echo "=== anomaly-detection (8003) ==="
	@curl -sf http://localhost:8003/health | python3 -m json.tool 2>/dev/null || echo "DOWN"
	@echo ""
	@echo "=== ais-spoof-detection (8005) ==="
	@curl -sf http://localhost:8005/health | python3 -m json.tool 2>/dev/null || echo "DOWN"
	@echo ""
	@echo "=== sts-detection (8006) ==="
	@curl -sf http://localhost:8006/health | python3 -m json.tool 2>/dev/null || echo "DOWN"
	@echo ""
	@echo "=== vessel-risk-engine (8007) ==="
	@curl -sf http://localhost:8007/health | python3 -m json.tool 2>/dev/null || echo "DOWN"
	@echo ""
	@echo "=== api-gateway (8015) ==="
	@curl -sf http://localhost:8015/health | python3 -m json.tool 2>/dev/null || echo "DOWN"
	@echo ""
	@echo "=== PIPELINE STATUS ==="
	@curl -sf http://localhost:8015/system/pipeline | python3 -m json.tool 2>/dev/null || echo "(gateway down)"
	@echo ""
	@echo "=== SYSTEM STATS ==="
	@curl -sf http://localhost:8015/system/stats | python3 -m json.tool 2>/dev/null || echo "(gateway down)"

# ── Database ───────────────────────────────────────────────────────────────────

db-shell: ## Open psql shell inside the postgres container
	docker compose exec postgres psql -U postgres -d maritime_oilspill

db-vessels: ## Quick query: show recent vessels
	docker compose exec postgres psql -U postgres -d maritime_oilspill \
	  -c "SELECT mmsi, vessel_name, vessel_type, last_seen, last_lat, last_lon FROM vessels ORDER BY last_seen DESC LIMIT 20;"

db-anomalies: ## Quick query: show recent anomaly events
	docker compose exec postgres psql -U postgres -d maritime_oilspill \
	  -c "SELECT mmsi, anomaly_type, severity, window_start FROM anomaly_events ORDER BY window_start DESC LIMIT 20;"

db-risk: ## Quick query: show vessel risk scores
	docker compose exec postgres psql -U postgres -d maritime_oilspill \
	  -c "SELECT mmsi, risk_score, tier, recommended_action, updated_at FROM vessel_risk_scores ORDER BY risk_score DESC LIMIT 20;"

db-sts: ## Quick query: show STS events
	docker compose exec postgres psql -U postgres -d maritime_oilspill \
	  -c "SELECT vessel_a, vessel_b, duration_minutes, avg_distance_m, confidence, start_time FROM sts_events ORDER BY start_time DESC LIMIT 10;"

# ── Redis ──────────────────────────────────────────────────────────────────────

redis-cli: ## Open redis-cli in the redis container
	docker compose exec redis redis-cli

redis-streams: ## List all Redis streams and their lengths
	docker compose exec redis redis-cli XLEN ais.clean
	docker compose exec redis redis-cli XLEN ais.features
	docker compose exec redis redis-cli XLEN anomaly.events
	docker compose exec redis redis-cli XLEN ais.trust
	docker compose exec redis redis-cli XLEN sts.events
	docker compose exec redis redis-cli XLEN vessel.risk

# ── Cleanup ────────────────────────────────────────────────────────────────────

clean: ## Remove containers, volumes, and images
	docker compose down -v --rmi local
