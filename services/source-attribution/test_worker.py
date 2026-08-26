import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SERVICE_ROOT.parent.parent
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def worker_module(monkeypatch):
    async def noop(*_args, **_kwargs):
        return None

    db_stub = types.ModuleType("shared.db.connection")
    db_stub.create_pool = noop
    db_stub.get_pool = noop
    db_stub.close_pool = noop
    redis_stub = types.ModuleType("shared.redis_client")
    redis_stub.close_redis = noop
    redis_stub.consume_stream = noop
    redis_stub.ensure_consumer_group = noop
    redis_stub.get_redis = noop
    redis_stub.publish_to_stream = noop
    monkeypatch.setitem(sys.modules, "shared.db.connection", db_stub)
    monkeypatch.setitem(sys.modules, "shared.redis_client", redis_stub)

    attribution_spec = importlib.util.spec_from_file_location(
        "source_attribution_attribution", SERVICE_ROOT / "app" / "attribution.py"
    )
    attribution = importlib.util.module_from_spec(attribution_spec)
    attribution_spec.loader.exec_module(attribution)
    monkeypatch.setitem(sys.modules, "app.attribution", attribution)

    worker_spec = importlib.util.spec_from_file_location(
        "source_attribution_worker", SERVICE_ROOT / "app" / "worker.py"
    )
    worker = importlib.util.module_from_spec(worker_spec)
    worker_spec.loader.exec_module(worker)
    return worker


class NoTrajectoryQueryPool:
    def __init__(self):
        self.executed = []

    async def fetchrow(self, *_args, **_kwargs):
        raise AssertionError("Source Attribution attempted a trajectory PostGIS query")

    async def fetch(self, *_args, **_kwargs):
        return []

    async def execute(self, *_args, **_kwargs):
        self.executed.append(_args)


def test_normalise_candidate_preserves_closest_approach(worker_module):
    candidate = worker_module._normalise_candidate_vessel({
        "vessel_id": 42,
        "mmsi": "123456789",
        "position_lat": 19.1,
        "position_lon": 72.8,
        "position_timestamp": "2024-08-20T09:40:00+00:00",
        "distance_m": 450.0,
        "closest_approach_m": 125.0,
        "time_gap_hours": 1.5,
        "high_anomaly_count": 2,
        "medium_anomaly_count": 3,
    })
    assert candidate["closest_approach_m"] == 125.0
    assert candidate["time_gap_hours"] == 1.5
    assert candidate["high_anomaly_count"] == 2
    assert candidate["medium_anomaly_count"] == 3


def test_score_trajectory_uses_incoming_closest_approach_without_query(worker_module, monkeypatch):
    observed = []
    monkeypatch.setattr(
        worker_module,
        "score_trajectory",
        lambda closest: observed.append(closest) or 0.75,
    )
    pool = NoTrajectoryQueryPool()
    vessel = {
        "vessel_id": 42,
        "mmsi": "123456789",
        "pos_lat": 19.1,
        "pos_lon": 72.8,
        "pos_ts": datetime(2024, 8, 20, 9, 40, tzinfo=timezone.utc),
        "distance_m": 450.0,
        "closest_approach_m": 125.0,
    }
    weather = {
        "wind_speed_ms": 0.0,
        "wind_dir_deg": 0.0,
        "current_speed_ms": 0.0,
        "current_dir_deg": 0.0,
    }

    result = asyncio.run(worker_module._score_and_persist_vessel(
        pool,
        "spill-1",
        19.1,
        72.8,
        vessel,
        datetime(2024, 8, 20, 9, 40, tzinfo=timezone.utc),
        weather,
    ))

    assert observed == [125.0]
    assert result["trajectory_score"] == 0.75
    assert len(pool.executed) == 1


def test_score_time_uses_incoming_time_gap_without_recalculating(worker_module, monkeypatch):
    observed = []
    monkeypatch.setattr(
        worker_module,
        "score_time",
        lambda gap: observed.append(gap) or 0.65,
    )
    async def no_anomalies(*_args):
        return 0, 0

    monkeypatch.setattr(worker_module, "score_behavior", lambda *_args: 0.0)
    monkeypatch.setattr(worker_module, "score_wind_drift", lambda **_kwargs: 0.0)
    monkeypatch.setattr(worker_module, "compute_attribution_score", lambda *_args: 0.0)
    monkeypatch.setattr(worker_module, "attribution_label", lambda _score: "test")

    class MinimalPool:
        async def execute(self, *_args):
            return None

    vessel = {
        "vessel_id": 42, "mmsi": "123456789", "pos_lat": 19.1, "pos_lon": 72.8,
        "pos_ts": datetime(2024, 8, 20, 9, 40, tzinfo=timezone.utc),
        "distance_m": 450.0, "closest_approach_m": 125.0, "time_gap_hours": 1.5,
    }
    weather = {
        "wind_speed_ms": 0.0, "wind_dir_deg": 0.0,
        "current_speed_ms": 0.0, "current_dir_deg": 0.0,
    }
    result = asyncio.run(worker_module._score_and_persist_vessel(
        MinimalPool(), "spill-1", 19.1, 72.8, vessel,
        datetime(2024, 8, 20, 9, 40, tzinfo=timezone.utc), weather,
    ))
    assert observed == [1.5]
    assert result["time_score"] == 0.65


def test_score_behavior_uses_incoming_anomaly_counts_without_query(worker_module, monkeypatch):
    observed = []
    monkeypatch.setattr(
        worker_module,
        "score_behavior",
        lambda high, medium: observed.append((high, medium)) or 0.55,
    )

    class NoAnomalyQueryPool:
        async def execute(self, *_args):
            return None

    vessel = {
        "vessel_id": 42, "mmsi": "123456789", "pos_lat": 19.1, "pos_lon": 72.8,
        "pos_ts": datetime(2024, 8, 20, 9, 40, tzinfo=timezone.utc),
        "distance_m": 450.0, "closest_approach_m": 125.0, "time_gap_hours": 1.5,
        "high_anomaly_count": 2, "medium_anomaly_count": 3,
    }
    weather = {
        "wind_speed_ms": 0.0, "wind_dir_deg": 0.0,
        "current_speed_ms": 0.0, "current_dir_deg": 0.0,
    }
    result = asyncio.run(worker_module._score_and_persist_vessel(
        NoAnomalyQueryPool(), "spill-1", 19.1, 72.8, vessel,
        datetime(2024, 8, 20, 9, 40, tzinfo=timezone.utc), weather,
    ))
    assert observed == [(2, 3)]
    assert result["behavior_score"] == 0.55


def test_score_wind_drift_uses_incoming_environment_without_query(worker_module, monkeypatch):
    observed = []
    monkeypatch.setattr(
        worker_module,
        "score_wind_drift",
        lambda **values: observed.append(values) or 0.45,
    )

    class NoEnvironmentQueryPool:
        async def execute(self, *_args):
            return None

    vessel = {
        "vessel_id": 42, "mmsi": "123456789", "pos_lat": 19.1, "pos_lon": 72.8,
        "pos_ts": datetime(2024, 8, 20, 8, 40, tzinfo=timezone.utc),
        "distance_m": 450.0, "closest_approach_m": 125.0, "time_gap_hours": 1.0,
        "high_anomaly_count": 0, "medium_anomaly_count": 0,
    }
    environment = {
        "wind_speed_ms": 10.0, "wind_dir_deg": 180.0,
        "current_speed_ms": 0.4, "current_dir_deg": 90.0,
    }
    result = asyncio.run(worker_module._score_and_persist_vessel(
        NoEnvironmentQueryPool(), "spill-1", 19.1, 72.8, vessel,
        datetime(2024, 8, 20, 9, 40, tzinfo=timezone.utc), environment,
    ))
    assert observed == [{
        "vessel_lat": 19.1, "vessel_lon": 72.8,
        "spill_lat": 19.1, "spill_lon": 72.8,
        "wind_speed_ms": 10.0, "wind_dir_deg": 180.0,
        "current_speed_ms": 0.4, "current_dir_deg": 90.0,
        "elapsed_hours": 1.0,
    }]
    assert result["wind_score"] == 0.45
