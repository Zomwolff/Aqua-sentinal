import importlib.util
import sys
import types
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SERVICE_ROOT.resolve().parent.parent
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(REPO_ROOT))

import app.evidence as evidence_mod  # noqa: E402

sys.modules.setdefault("app.evidence", evidence_mod)


def _load_worker():
    async def _noop(*_a, **_k):
        return None

    stub_keys = ["shared.redis_client", "shared.db.connection"]
    previous = {key: sys.modules.get(key) for key in stub_keys}

    rstub = types.ModuleType("shared.redis_client")
    rstub.consume_stream = _noop
    rstub.ensure_consumer_group = _noop
    rstub.get_redis = _noop
    rstub.publish_to_stream = _noop
    sys.modules["shared.redis_client"] = rstub

    dbstub = types.ModuleType("shared.db.connection")
    dbstub.create_pool = _noop
    sys.modules["shared.db.connection"] = dbstub

    spec = importlib.util.spec_from_file_location("app.evidence_worker_prov", str(SERVICE_ROOT / "app" / "worker.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        for key in stub_keys:
            if previous.get(key) is not None:
                sys.modules[key] = previous[key]
            else:
                sys.modules.pop(key, None)
        sys.modules.pop(spec.name, None)
        sys.modules.pop("app.evidence", None)
    return mod


_WORKER = _load_worker()


def _payload(is_synthetic):
    return {
        "candidate_id": "c-1",
        "scene_id": "COPERNICUS/S1_GRD/S1A_P",
        "confidence": 0.63,
        "classification_label": "possible_oil_spill",
        "acquisition_time": "2024-08-20T09:40:00.000Z",
        "is_synthetic": is_synthetic,
    }


def test_candidate_from_payload_preserves_synthetic():
    for flag in (True, False):
        candidate = _WORKER._candidate_from_payload(_payload(flag))
        assert candidate["is_synthetic"] is flag


def test_candidate_from_payload_synthetic_defaults_false_when_absent():
    payload = _payload(False)
    payload.pop("is_synthetic")
    candidate = _WORKER._candidate_from_payload(payload)
    assert candidate["is_synthetic"] is False


def test_fuse_evidence_carries_synthetic_into_incident_fused():
    for flag in (True, False):
        candidate = _WORKER._candidate_from_payload(_payload(flag))
        event = evidence_mod.fuse_evidence(candidate, None)
        # Provenance is inherited from the SAR candidate, never from a vessel.
        assert event["is_synthetic"] is flag
        assert event["classification_label"] == "possible_oil_spill"
        assert event["correlated_vessel_id"] is None