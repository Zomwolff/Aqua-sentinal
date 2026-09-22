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


@pytest.mark.parametrize("as_directory", [False, True])
def test_probability_fusion_is_independent_of_upload_format(monkeypatch, tmp_path, as_directory):
    import fusion.pipeline as pipeline
    import fusion.onnx_runtime as runtime
    eo = tmp_path / "eo"
    eo.mkdir() if as_directory else eo.touch()
    sar_probs = np.array([[.9, .8], [.2, .9]], np.float32)
    eo_probs = np.array([[.2, .1], [.9, .9]], np.float32)
    valid = np.array([[True, True], [True, False]])
    monkeypatch.setattr(pipeline, "_require_models", lambda *_: None)
    monkeypatch.setattr(runtime, "predict_sar_probability", lambda *_: (sar_probs, valid))
    monkeypatch.setattr(runtime, "predict_eo_probability", lambda *_: (eo_probs, valid))
    monkeypatch.setattr(runtime, "align_probability_pair", lambda *_: (sar_probs, valid))
    result = pipeline._detect_oil_result("sar.tif", eo)
    np.testing.assert_array_equal(result["mask"], [[1, 0], [1, 0]])
    np.testing.assert_allclose(result["probability"], [[.55, .45], [.55, 0]])
    assert result["weights"] == {"sar": .5, "eo": .5}
    assert result["mask"].dtype == np.uint8
    assert result["sar_only_rejected_pixel_count"] == 1
    mask, _, _ = runtime.detect_oil_onnx("sar.tif", eo, Path("sar.onnx"), Path("eo.onnx"))
    np.testing.assert_array_equal(mask, result["mask"])


def test_eo_threshold_is_point_four(monkeypatch):
    import fusion.pipeline as pipeline
    import fusion.onnx_runtime as runtime
    probabilities = np.array([[.1, .3999, .4, .8]], np.float32)
    valid = np.array([[True, True, True, False]])
    monkeypatch.setattr(pipeline, "_require_models", lambda *_: None)
    monkeypatch.setattr(runtime, "predict_eo_probability", lambda *_: (probabilities, valid))
    result = pipeline.detect_oil_result(sentinel2_path="eo.tif")
    np.testing.assert_array_equal(result["mask"], [[0, 0, 1, 0]])
    assert result["threshold"] == .4
