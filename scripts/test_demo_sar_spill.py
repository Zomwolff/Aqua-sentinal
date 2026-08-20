"""Lightweight validation for scripts/demo_sar_spill.py pure helpers.

These tests exercise response parsing, candidate filtering by scene_id, and
synthetic provenance verification WITHOUT requiring Docker / Redis / PostGIS.
The live I/O paths (SARDemoClient) are excluded here; they are validated by
running the script against the real stack.
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from demo_sar_spill import (  # noqa: E402
    parse_sar_clean_message,
    filter_by_scene,
    candidate_ids_from_raw_event,
    check_synthetic_provenance,
    verify_provenance_chain,
    timeout_guard,
    SARDemoError,
)
import pytest  # noqa: E402


def test_parse_sar_clean_string_metadata():
    msg = {
        "id": "1-0",
        "data": {
            "scene_metadata": '{"scene_id": "S1_X", "is_synthetic": true, '
                               '"acquisition_time": "2024-01-01T00:00:00Z"}',
            "raster_path": "/data/sar/S1_X.tif",
        },
    }
    out = parse_sar_clean_message(msg)
    assert out["scene_id"] == "S1_X"
    assert out["is_synthetic"] is True
    assert out["acquisition_time"] == "2024-01-01T00:00:00Z"
    assert out["raster_path"] == "/data/sar/S1_X.tif"


def test_parse_sar_clean_dict_metadata():
    msg = {"data": {"scene_metadata": {"scene_id": "S2_Y", "is_synthetic": False}}}
    out = parse_sar_clean_message(msg)
    assert out["scene_id"] == "S2_Y"
    assert out["is_synthetic"] is False


def test_filter_by_scene():
    items = [
        {"scene_id": "A", "candidate_id": "c1"},
        {"scene_id": "B", "candidate_id": "c2"},
        {"scene_id": "A", "candidate_id": "c3"},
    ]
    got = filter_by_scene(items, "A")
    assert [c["candidate_id"] for c in got] == ["c1", "c3"]
    assert filter_by_scene(items, "Z") == []


def test_candidate_ids_from_raw_event():
    msg = {"data": {"candidate_ids": '["c1", "c2"]', "scene_id": "A"}}
    assert candidate_ids_from_raw_event(msg) == ["c1", "c2"]
    msg2 = {"data": {"candidate_ids": ["c3"], "scene_id": "A"}}
    assert candidate_ids_from_raw_event(msg2) == ["c3"]
    # Missing field -> empty list, never crash
    assert candidate_ids_from_raw_event({"data": {}}) == []


def test_check_synthetic_provenance_true():
    assert check_synthetic_provenance({"is_synthetic": True}, expected=True) is True
    assert check_synthetic_provenance({"is_synthetic": "true"}, expected=True) is True
    # Field says real but we expected synthetic -> mismatch
    assert check_synthetic_provenance({"is_synthetic": False}, expected=True) is False


def test_check_synthetic_provenance_false():
    assert check_synthetic_provenance({"is_synthetic": False}, expected=False) is True
    assert check_synthetic_provenance({"is_synthetic": True}, expected=False) is False


def test_verify_provenance_chain():
    boundaries = {
        "sar.clean": {"is_synthetic": True},
        "spill_candidates": {"is_synthetic": True},
        "spill.candidates.filtered": {"is_synthetic": True},
        "incident.fused": {"is_synthetic": True},
    }
    report = verify_provenance_chain(boundaries, expected_synthetic=True)
    assert all(report.values()), report

    # A dropped/missing boundary fails
    boundaries["incident.fused"] = None
    report = verify_provenance_chain(boundaries, expected_synthetic=True)
    assert report["incident.fused"] is False

    # A boundary that flipped to real while we expected synthetic fails
    boundaries["incident.fused"] = {"is_synthetic": False}
    report = verify_provenance_chain(boundaries, expected_synthetic=True)
    assert report["incident.fused"] is False


def test_timeout_guard():
    # Not yet exceeded (started just now)
    timeout_guard(time.time(), 10.0, "stage-x")  # should not raise
    with pytest.raises(TimeoutError):
        timeout_guard(time.time() - 11.0, 10.0, "stage-y")


def test_provenance_never_inferred_from_scene_id():
    # Even a suspicious scene_id must not be treated as synthetic.
    candidate = {"scene_id": "SYNTHETIC_S1", "is_synthetic": False}
    assert check_synthetic_provenance(candidate, expected=True) is False
    assert check_synthetic_provenance(candidate, expected=False) is True
