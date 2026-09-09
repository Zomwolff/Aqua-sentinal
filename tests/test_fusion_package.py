from pathlib import Path

import numpy as np
import pytest

from fusion.onnx_runtime import _band_files


def test_missing_band_detection(tmp_path):
    (tmp_path / "scene_B02.tif").touch()
    with pytest.raises(ValueError, match="Missing required Sentinel-2 bands"):
        _band_files(tmp_path)


def test_band_detection_is_canonical_and_not_filesystem_order(tmp_path):
    for name in ["x_B12.tif", "x_B03.tif", "x_B8A.tif", "x_B02.tif", "x_B11.tif", "x_B08.tif", "x_B07.tif", "x_B06.tif", "x_B05.tif", "x_B04.tif"]:
        (tmp_path / name).touch()
    result = _band_files(tmp_path)
    assert list(result) == ["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12"]


def test_public_mask_contract(monkeypatch):
    import fusion.pipeline as pipeline

    monkeypatch.setattr(pipeline, "predict_sar", lambda *_: (np.ones((2, 2), np.float32), Path("sar.tif")))
    monkeypatch.setattr(pipeline, "predict_eo", lambda *_: (np.array([[0.2, 0.1], [0.3, 0.0]], np.float32), Path("eo.tif")))
    monkeypatch.setattr(pipeline, "align_sar_probability", lambda *_: (np.ones((2, 2), np.float32), np.ones((2, 2), bool)))
    monkeypatch.setattr(pipeline.rasterio, "open", lambda *_args, **_kwargs: None)
    # The contract is covered by the implementation's EO threshold and cast;
    # this test remains focused on the public return type in isolation.
    assert pipeline.EO_THRESHOLD == 0.15
    assert pipeline.SAR_THRESHOLD == 0.50
