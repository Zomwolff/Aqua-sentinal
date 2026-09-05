import importlib.util
from datetime import datetime, timezone
from pathlib import Path


SEED_PATH = Path(__file__).with_name("seed_demo_data.py")
SPEC = importlib.util.spec_from_file_location("seed_demo_data", SEED_PATH)
seed_demo_data = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(seed_demo_data)


def test_synthetic_sts_records_are_recent_and_schema_compatible():
    now = datetime.now(timezone.utc)

    assert len(seed_demo_data.SYNTHETIC_STS_EVENTS) == 3
    for a_idx, b_idx, duration, avg_distance, min_distance, confidence, lon, lat in seed_demo_data.SYNTHETIC_STS_EVENTS:
        assert a_idx != b_idx
        assert 0 <= a_idx < len(seed_demo_data.VESSELS)
        assert 0 <= b_idx < len(seed_demo_data.VESSELS)
        assert duration > 0
        assert 0 < min_distance <= avg_distance <= 500
        assert 0 < confidence <= 1
        assert -180 <= lon <= 180
        assert -90 <= lat <= 90
        start_time = seed_demo_data.NOW.replace(tzinfo=timezone.utc)
        assert (now - start_time).total_seconds() < 48 * 3600


def test_synthetic_spoofing_records_are_below_api_threshold_and_distinct():
    records = seed_demo_data.SYNTHETIC_TRUST_SCORES

    assert len(records) == 3
    assert {record[0] for record in records} == {0, 1, 2}
    assert {record[2] for record in records} == {0.24, 0.46, 0.12}
    assert all(record[2] < 0.5 for record in records)
    assert any(record[4] for record in records)
    assert any(record[5] for record in records)
    assert any(not record[6] for record in records)


def test_reseeding_resets_intelligence_tables_for_idempotency():
    assert "ais_trust_scores" in seed_demo_data.DEMO_RESET_TABLES
    assert "sts_events" in seed_demo_data.DEMO_RESET_TABLES
    assert "vessel_risk_scores" in seed_demo_data.DEMO_RESET_TABLES


def test_existing_scorer_exposes_seeded_sts_and_trust_factors():
    sts_events = [
        {"start_time": seed_demo_data.NOW.isoformat(), "end_time": seed_demo_data.NOW.isoformat()}
    ]

    risk = seed_demo_data.compute_risk_score(
        seed_demo_data.VESSELS[0][0], [], 0.24, False, sts_events, "tanker"
    )
    factors = {factor["factor"]: factor for factor in risk["contributing_factors"]}

    assert factors["sts"]["contribution"] == 10.0
    assert factors["trust"]["contribution"] == 19.0
