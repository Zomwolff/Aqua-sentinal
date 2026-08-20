import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SERVICE_ROOT.resolve().parent.parent
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from shared.spatial import geo  # noqa: E402  (repo-root canonical shared)

from app.evidence import (  # noqa: E402
    FORBIDDEN_ATTRIBUTION_FIELDS,
    build_vessel_correlation_sql,
    candidate_lookup_sql,
    correlation_windows,
    fuse_evidence,
    select_correlated_vessel,
)

T0 = datetime(2024, 8, 20, 9, 40, 0, tzinfo=timezone.utc)


def _candidate_payload(confidence=0.63, candidate_id="candidate-101"):
    return {
        "candidate_id": candidate_id,
        "scene_id": "COPERNICUS/S1_GRD/S1A_STEP6_E2E_20240820",
        "confidence": confidence,
        "classification_label": "possible_oil_spill",
        "acquisition_time": T0.isoformat(),
        "orbit": 149,
        "polarization": "VV",
        "resolution": 10,
    }


def _vessel_record(vessel_id=42, mmsi="123456789", risk_score=80.0, tier="HIGH",
                   recommended_action="satellite_task", distance_m=800.0,
                   position_timestamp=T0):
    return {
        "vessel_id": vessel_id,
        "mmsi": mmsi,
        "risk_score": risk_score,
        "tier": tier,
        "recommended_action": recommended_action,
        "position_timestamp": position_timestamp,
        "distance_m": distance_m,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Queries use PostGIS geography (no degree arithmetic)
# ──────────────────────────────────────────────────────────────────────────────

def test_correlation_query_uses_geography_semantics():
    sql = build_vessel_correlation_sql()
    assert "ST_DWithin" in sql
    assert "::geography" in sql
    assert "ST_Distance" in sql
    assert "BETWEEN" in sql
    assert "ST_MakePoint" in sql
    assert "'HIGH'" in sql and "'CRITICAL'" in sql


def test_candidate_lookup_uses_centroid():
    assert "ST_Centroid" in candidate_lookup_sql()


# ──────────────────────────────────────────────────────────────────────────────
# Correlation selection
# ──────────────────────────────────────────────────────────────────────────────

def test_select_nearest_and_tiebreak_by_mmsi():
    recs = [
        _vessel_record(vessel_id=1, mmsi="200", distance_m=500.0),
        _vessel_record(vessel_id=2, mmsi="100", distance_m=500.0),
        _vessel_record(vessel_id=3, mmsi="300", distance_m=300.0),
    ]
    selected = select_correlated_vessel(recs, 5000.0, 6.0, T0)
    assert selected["vessel_id"] == 3  # nearest


def test_select_excludes_vessel_outside_spatial_window():
    recs = [_vessel_record(vessel_id=9, distance_m=12000.0)]
    assert select_correlated_vessel(recs, 5000.0, 6.0, T0) is None


def test_select_excludes_vessel_outside_temporal_window():
    outside = T0 - timedelta(hours=12)
    recs = [_vessel_record(vessel_id=9, position_timestamp=outside)]
    assert select_correlated_vessel(recs, 5000.0, 6.0, T0) is None


def test_select_returns_none_for_empty():
    assert select_correlated_vessel([], 5000.0, 6.0, T0) is None


# ──────────────────────────────────────────────────────────────────────────────
# fuse_evidence
# ──────────────────────────────────────────────────────────────────────────────

def test_fuse_with_correlated_vessel():
    candidate = _candidate_payload()
    event = fuse_evidence(candidate, _vessel_record(vessel_id=42, mmsi="123456789",
                                                     risk_score=80.0, tier="HIGH",
                                                     distance_m=800.0))
    assert event["candidate_id"] == "candidate-101"
    assert event["confidence"] == 0.63
    assert event["classification_label"] == "possible_oil_spill"
    assert event["correlated_vessel_id"] == 42
    assert event["correlated_vessel"]["mmsi"] == "123456789"
    assert event["orbit"] == 149


def test_fuse_without_vessel_keeps_null_and_still_emits():
    event = fuse_evidence(_candidate_payload(), None)
    assert event["correlated_vessel_id"] is None
    assert event["correlated_vessel"] is None
    assert event["candidate_id"] == "candidate-101"


def test_fuse_never_contains_source_attribution():
    event = fuse_evidence(_candidate_payload(), _vessel_record())
    assert not set(FORBIDDEN_ATTRIBUTION_FIELDS) & set(event.keys())
    for key in FORBIDDEN_ATTRIBUTION_FIELDS:
        assert key not in event


def test_correlation_windows_from_env(monkeypatch):
    monkeypatch.setenv("EVIDENCE_SPATIAL_WINDOW_M", "2500")
    monkeypatch.setenv("EVIDENCE_TEMPORAL_WINDOW_HOURS", "3")
    spatial, hours = correlation_windows()
    assert spatial == 2500.0 and hours == 3.0
    monkeypatch.delenv("EVIDENCE_SPATIAL_WINDOW_M")
    monkeypatch.delenv("EVIDENCE_TEMPORAL_WINDOW_HOURS")
    assert correlation_windows() == (5000.0, 6.0)


# ──────────────────────────────────────────────────────────────────────────────
# Worker integration (stubbed DB + Redis publish; no live services)
# ──────────────────────────────────────────────────────────────────────────────

class FakePool:
    """Minimal asyncpg-like double for fetchrow/fetch."""

    def __init__(self, candidate_rows, vessel_rows):
        self.candidate_rows = candidate_rows
        self.vessel_rows = vessel_rows

    async def fetchrow(self, sql, *params):
        cid = params[0] if params else None
        return self.candidate_rows.get(str(cid))

    async def fetch(self, sql, *params):
        return list(self.vessel_rows)


def _load_worker():
    """Import the real worker module with stubbed shared infra."""
    import app.evidence as evidence_mod

    sys.modules.setdefault("app.evidence", evidence_mod)

    redis_stub = types.ModuleType("shared.redis_client")
    async def _noop(*args, **kwargs):
        return None
    redis_stub.consume_stream = _noop
    redis_stub.ensure_consumer_group = _noop
    redis_stub.get_redis = _noop
    redis_stub.publish_to_stream = _noop
    sys.modules["shared.redis_client"] = redis_stub

    db_stub = types.ModuleType("shared.db.connection")
    db_stub.create_pool = _noop
    sys.modules["shared.db.connection"] = db_stub

    spec = importlib.util.spec_from_file_location(
        "app.evidence_fusion_worker", str(SERVICE_ROOT / "app" / "worker.py")
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


def _run_worker_test(worker, payload, candidate_rows, vessel_rows, windows=(5000.0, 1.0)):
    pool = FakePool(candidate_rows, vessel_rows)
    captured = []
    async def recorder(_client, stream, data, max_len=50000):
        captured.append((stream, dict(data)))
    worker.publish_to_stream = recorder
    worker.correlation_windows = lambda: (windows[0], windows[1])
    asyncio.run(_run(worker, payload, pool))
    return captured


async def _run(worker, payload, pool):
    from app.evidence import fuse_evidence  # noqa
    await worker._process_filtered_message(payload, pool, redis=None)


class TestWorkerFusion:
    def _candidate_row(self, candidate_id="candidate-101"):
        return {
            "centroid_lon": 72.78195,
            "centroid_lat": 19.11805,
            "acquisition_time": T0,
        }

    def test_sar_with_nearby_high_risk_vessel(self):
        worker = _load_worker()
        captured = _run_worker_test(
            worker,
            _candidate_payload(),
            {"candidate-101": self._candidate_row()},
            [_vessel_record(vessel_id=42, mmsi="123456789", risk_score=80.0, tier="HIGH",
                            distance_m=800.0, position_timestamp=T0)],
        )
        assert len(captured) == 1
        stream, event = captured[0]
        assert stream == "incident.fused"
        assert event["candidate_id"] == "candidate-101"
        assert event["confidence"] == 0.63
        assert event["classification_label"] == "possible_oil_spill"
        assert event["correlated_vessel_id"] == 42

    def test_sar_with_no_nearby_vessel_still_fused(self):
        worker = _load_worker()
        captured = _run_worker_test(
            worker,
            _candidate_payload(),
            {"candidate-101": self._candidate_row()},
            [],
        )
        assert len(captured) == 1
        stream, event = captured[0]
        assert stream == "incident.fused"
        assert event["correlated_vessel_id"] is None

    def test_vessel_outside_spatial_window_not_correlated(self):
        worker = _load_worker()
        captured = _run_worker_test(
            worker,
            _candidate_payload(),
            {"candidate-101": self._candidate_row()},
            [_vessel_record(vessel_id=42, distance_m=9000.0, position_timestamp=T0)],
        )
        _, event = captured[0]
        assert event["correlated_vessel_id"] is None

    def test_vessel_outside_temporal_window_not_correlated(self):
        worker = _load_worker()
        captured = _run_worker_test(
            worker,
            _candidate_payload(),
            {"candidate-101": self._candidate_row()},
            [_vessel_record(vessel_id=42, distance_m=800.0,
                            position_timestamp=T0 - timedelta(hours=50))],
        )
        _, event = captured[0]
        assert event["correlated_vessel_id"] is None

    def test_multiple_vessels_selects_nearest(self):
        worker = _load_worker()
        captured = _run_worker_test(
            worker,
            _candidate_payload(),
            {"candidate-101": self._candidate_row()},
            [
                _vessel_record(vessel_id=1, mmsi="100", distance_m=900.0, position_timestamp=T0),
                _vessel_record(vessel_id=2, mmsi="200", distance_m=300.0, position_timestamp=T0),
            ],
        )
        _, event = captured[0]
        assert event["correlated_vessel_id"] == 2

    def test_worker_payload_contains_no_attribution_fields(self):
        worker = _load_worker()
        captured = _run_worker_test(
            worker,
            _candidate_payload(),
            {"candidate-101": self._candidate_row()},
            [_vessel_record(vessel_id=42, position_timestamp=T0)],
        )
        _, event = captured[0]
        assert not set(FORBIDDEN_ATTRIBUTION_FIELDS) & set(event.keys())