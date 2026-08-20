import importlib.util
import sys
import types
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SERVICE_ROOT.resolve().parent.parent
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "services"))


def _load_worker():
    async def _noop(*_a, **_k):
        return None

    stub_keys = ["shared.redis_client", "shared.db.connection", "shared.artifacts"]
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

    artstub = types.ModuleType("shared.artifacts")
    artstub.load_scene_artifact = lambda *a, **k: None
    artstub.crop_region = lambda *a, **k: None
    artstub.geometry_to_pixel_bbox = lambda *a, **k: (0, 0, 0, 0)
    sys.modules["shared.artifacts"] = artstub

    spec = importlib.util.spec_from_file_location("app.lookalike_worker_prov", str(SERVICE_ROOT / "app" / "worker.py"))
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
    return mod


_WORKER = _load_worker()


def _data(is_synthetic, candidate_id="c-1"):
    return {
        "candidate_id": candidate_id,
        "scene_id": "COPERNICUS/S1_GRD/S1A_P",
        "acquisition_time": "2024-08-20T09:40:00.000Z",
        "is_synthetic": is_synthetic,
        "orbit": 149,
    }


def _result(candidate_id="c-1"):
    return {
        "candidate_id": candidate_id,
        "scene_id": "COPERNICUS/S1_GRD/S1A_P",
        "confidence": 0.63,
        "classification_label": "possible_oil_spill",
    }


def test_filtered_event_carries_synthetic_flag_for_true_and_false():
    for flag in (True, False):
        event = _WORKER._filtered_event(_data(flag), _result())
        assert event["is_synthetic"] is flag
        assert event["candidate_id"] == "c-1"
        assert event["classification_label"] == "possible_oil_spill"


def test_filtered_event_synthetic_defaults_false_when_absent():
    data = _data(False)
    data.pop("is_synthetic")
    event = _WORKER._filtered_event(data, _result())
    assert event["is_synthetic"] is False