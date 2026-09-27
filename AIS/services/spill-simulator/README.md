# Oil Spill Simulator Service

Real-time AIS data injection service for simulating oil spill scenarios with SAR+EO fusion detection.

## Features

- **Historical Accuracy**: Based on real oil spill events (Wakashio 2020)
- **13 Vessels**: 1 culprit, 2 suspicious, 10 normal traffic
- **60x Speed**: Compress 6-hour scenarios into 6 minutes
- **Redis Control**: Start/stop via Redis pub/sub
- **Live Monitoring**: Real-time status updates

## Architecture

```
Redis Control Channel → Simulator → AIS Stream → Pipeline
  control.simulate_spill   main.py   ais.positions   (detection)
```

## Quick Start

### 1. Start with Docker Compose

```bash
# From project root
docker-compose up spill-simulator
```

### 2. Trigger Simulation

**Via UI:**
- Open http://localhost:3000
- Click "Simulate Oil Spill" button

**Via API:**
```bash
curl -X POST http://localhost:8015/api/simulate/oil-spill \
  -H "Content-Type: application/json" \
  -d '{"scenario":"wakashio_fusion_demo","speed_multiplier":60}'
```

**Via Redis:**
```bash
redis-cli -p 6380 PUBLISH control.simulate_spill '{"scenario":"wakashio_fusion_demo","speed_multiplier":60}'
```

### 3. Monitor Progress

```bash
# Status endpoint
curl http://localhost:8015/api/simulate/status

# Redis events
redis-cli -p 6380 XREAD STREAMS simulation.status 0
```

## Scenarios

### Wakashio Fusion Demo

- **File**: `../simulation_scenarios/wakashio_fusion_demo.csv`
- **Vessels**: 13 (699 AIS records)
- **Location**: Mauritius (-20.442, 57.745)
- **Date**: 2020-08-09
- **Duration**: 6 hours (360 minutes) → 6 minutes at 60x

### Culprit Vessel

**MMSI**: 999888001 (MT SUSPICIOUS)

**Anomalies**:
- 90-minute AIS gap (09:30-11:00)
- Loitering at spill site (45 minutes)
- Erratic course changes
- Direct trajectory through spill coordinates

## Configuration

Environment variables (set in `docker-compose.yml` or `.env`):

```bash
# Redis connection
REDIS_HOST=redis
REDIS_PORT=6379

# Simulation parameters
SIMULATION_SPEED_MULTIPLIER=60        # Real-time multiplier
SCENARIO_PATH=/app/scenarios/wakashio_fusion_demo.csv

# Database (for vessel metadata lookups)
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=aqua_sentinel
```

## Testing

### Automated Test

```bash
# Install dependencies
pip install httpx redis pytest

# Run full end-to-end test
python test_fusion_simulation.py
```

Tests validate:
1. ✅ Simulation start
2. ✅ AIS injection (13 vessels)
3. ✅ Anomaly detection
4. ✅ SAR+EO tasking
5. ✅ Fusion processing (85%+ confidence)
6. ✅ Spill detection
7. ✅ Culprit attribution

### Quick Test

```bash
./quick_test.sh
```

Performs rapid health check and simulation validation.

## Redis Channels

### Control Channels (Input)

| Channel | Purpose | Message Format |
|---------|---------|----------------|
| `control.simulate_spill` | Start simulation | `{"scenario": "wakashio_fusion_demo", "speed_multiplier": 60}` |
| `control.stop_simulation` | Stop running simulation | `{"reason": "user_request"}` |

### Status Channels (Output)

| Channel | Purpose | Message Format |
|---------|---------|----------------|
| `simulation.status` | Progress updates (stream) | `{"status": "running", "progress": 0.45, ...}` |
| `ais.positions` | AIS position stream | `{"mmsi": "999888001", "latitude": -20.442, ...}` |

## File Structure

```
spill-simulator/
├── app/
│   └── main.py              # Main simulator service
├── Dockerfile               # Container definition
├── requirements.txt         # Python dependencies
├── test_fusion_simulation.py # End-to-end tests
├── quick_test.sh            # Quick validation script
└── README.md                # This file

../simulation_scenarios/
├── wakashio_fusion_demo.csv          # 13-vessel scenario data
└── generate_wakashio_fusion_scenario.py  # Scenario generator
```

## Troubleshooting

### Simulation doesn't start

1. Check Redis connection:
   ```bash
   redis-cli -p 6380 ping
   ```

2. Verify scenario file exists:
   ```bash
   docker-compose exec spill-simulator ls -la /app/scenarios/
   ```

3. Check logs:
   ```bash
   docker-compose logs spill-simulator
   ```

### No AIS data received

1. Monitor Redis stream:
   ```bash
   redis-cli -p 6380 SUBSCRIBE ais.positions
   ```

2. Verify speed multiplier isn't too high:
   ```bash
   # Lower to 30x if processing can't keep up
   SIMULATION_SPEED_MULTIPLIER=30
   ```

### Simulation too fast/slow

Adjust speed multiplier:
- **Too fast** (processing delays): Set to 30-40x
- **Too slow** (demo): Set to 120x (3 minutes)
- **Production**: Set to 10x (36 minutes)

## API Integration

The simulator integrates with:

1. **API Gateway** (`../api-gateway`): HTTP endpoints for UI
2. **AIS Reader** (`../ais-reader`): Consumes AIS positions
3. **Anomaly Detection** (`../anomaly-detection`): Flags suspicious vessels
4. **SAR Service** (`sar-cfar/`): Triggers satellite tasking
5. **Fusion Service** (`fusion/`): Processes SAR+EO imagery

## Performance

Expected performance on standard hardware:

- **Injection Rate**: ~2 positions/second at 60x
- **Memory**: ~100 MB
- **CPU**: 5-10%
- **Duration**: 6 minutes for full scenario

## Creating Custom Scenarios

1. **Generate CSV**:
   ```bash
   cd ../simulation_scenarios
   python generate_wakashio_fusion_scenario.py \
     --vessels 13 \
     --output custom_scenario.csv
   ```

2. **Update config**:
   ```bash
   SCENARIO_PATH=/app/scenarios/custom_scenario.csv
   ```

3. **Restart service**:
   ```bash
   docker-compose restart spill-simulator
   ```

## License

MIT License - see project root LICENSE file

## Support

- **Docs**: `/docs/oil-spill-fusion-simulation.md`
- **Issues**: GitHub Issues
- **Email**: support@aquasentinel.io
