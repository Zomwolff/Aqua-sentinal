import importlib.util
import sys
import types
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SERVICE_ROOT.resolve().parent.parent
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(REPO_ROOT))


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
    artstub.save_scene_artifact = lambda *a, **k: "/artifacts/x"
    sys.modules["shared.artifacts"] = artstub

    spec = importlib.util.spec_from_file_location("app.sar_worker_prov", str(SERVICE_ROOT / "app" / "worker.py"))
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


def _meta(is_synthetic):
    return {
        "scene_id": "COPERNICUS/S1_GRD/S1A_P", 
        "acquisition_time": "2024-08-20T09:40:00.000Z",
        "is_synthetic": is_synthetic,
    }


def test_parse_scene_metadata_preserves_synthetic():
    parsed = _WORKER._parse_scene_metadata(_meta(True))
    assert parsed["is_synthetic"] is True
    parsed = _WORKER._parse_scene_metadata(_meta(False))
    assert parsed["is_synthetic"] is False


def test_is_synthetic_normalizes_and_defaults_false():
    assert _WORKER._is_synthetic({}) is False
    assert _WORKER._is_synthetic({"is_synthetic": "true"}) is True
    assert _WORKER._is_synthetic({"is_synthetic": True}) is True
    assert _WORKER._is_synthetic({"is_synthetic": "0"}) is False


def test_candidates_raw_event_carries_synthetic():
    candidates = [{"candidate_id": "c1", "geometry": {}, "area_m2": 1.0, "pixel_count": 1}]
    for flag in (True, False):
        event = _WORKER._candidates_raw_event(_meta(flag), candidates)
        assert event["is_synthetic"] is flag
        assert event["candidate_ids"] == ["c1"]


def test_insert_sql_sets_is_synthetic():
    assert "is_synthetic" in _WORKER._INSERT_CANDIDATE_SQL
    assert "$8" in _WORKER._INSERT_CANDIDATE_SQL


def test_worker_uses_unet_probability_for_candidates(tmp_path, monkeypatch):
    import asyncio
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "sar.tif"
    with rasterio.open(path, "w", driver="GTiff", width=8, height=8,
                       count=2, dtype="float32", crs="EPSG:4326",
                       transform=from_origin(75, 15, .001, .001)) as dst:
        dst.write(np.full((2, 8, 8), -20, np.float32))

    probabilities = np.full((8, 8), .49, np.float32)
    probabilities[2:4, 2:4] = .51
    observed = {}

    def predict(source, model):
        assert source == str(path)
        assert model == _WORKER.SAR_ONNX
        return probabilities, np.ones((8, 8), bool)

    def extract(**kwargs):
        observed["mask"] = kwargs["mask"].copy()
        return []

    async def publish(*args, **kwargs):
        return None

    async def execute(*args, **kwargs):
        return None

    monkeypatch.setattr(_WORKER, "predict_sar_probability", predict)
    monkeypatch.setattr(_WORKER, "extract_candidates", extract)
    monkeypatch.setattr(_WORKER, "publish_to_stream", publish)
    monkeypatch.setattr(_WORKER, "save_scene_artifact", lambda *a, **k: str(tmp_path / "artifact"))
    monkeypatch.setattr(_WORKER.asyncio, "sleep", publish)
    monkeypatch.delenv("SAR_BRIGHT_TARGET_THRESHOLD", raising=False)
    pool = types.SimpleNamespace(execute=execute)
    data = {"raster_path": str(path), "scene_metadata": _meta(False)}
    asyncio.run(_WORKER._process_sar_message(data, pool, object()))
    expected = np.zeros((8, 8), bool)
    expected[2:4, 2:4] = True
    np.testing.assert_array_equal(observed["mask"], expected)
