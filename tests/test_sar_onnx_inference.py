import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from fusion import onnx_runtime as runtime


def write_sar(path, values, nodata=None):
    with rasterio.open(path, "w", driver="GTiff", count=values.shape[0],
                       height=values.shape[1], width=values.shape[2], dtype="float32",
                       crs="EPSG:32640", transform=from_origin(500000, 2800000, 10, 10),
                       nodata=nodata) as dst:
        dst.write(values.astype(np.float32))


@pytest.mark.parametrize("channels", [1, 3])
def test_rejects_old_grayscale_and_rgb_inputs(tmp_path, channels):
    path = tmp_path / "sar.tif"
    write_sar(path, np.full((channels, 8, 8), -20))
    with pytest.raises(ValueError, match="two Sigma0-dB bands"):
        runtime._load_sar_image(path)


def test_frozen_normalization_preserves_band_positions(tmp_path):
    path = tmp_path / "sar.tif"
    values = np.stack([np.full((8, 8), -20), np.full((8, 8), -10)])
    write_sar(path, values)
    normalized, valid = runtime._load_sar_image(path)
    np.testing.assert_allclose(normalized[0], (-20 + 41.22778857146995) / 41.22778857146995)
    np.testing.assert_allclose(normalized[1], (-10 + 34.39919176014351) / 34.39919176014351)
    assert valid.all()
    assert normalized.dtype == np.float32


def test_nonfinite_sar_is_excluded(tmp_path):
    path = tmp_path / "sar.tif"
    values = np.full((2, 8, 8), -20.0)
    values[1, 0, 0] = np.nan
    write_sar(path, values)
    normalized, valid = runtime._load_sar_image(path)
    assert not valid[0, 0]
    assert np.isfinite(normalized).all()


@pytest.mark.parametrize("shape", [(1, 1), (32, 48), (520, 600)])
def test_stitches_probabilities_and_crops_padding(monkeypatch, tmp_path, shape):
    path = tmp_path / "sar.tif"
    values = np.full((2, *shape), -20.0)
    if shape != (1, 1):
        values[:, 0, 0] = -9999
    write_sar(path, values, nodata=-9999)

    class Session:
        def get_inputs(self):
            return [type("Input", (), {"name": "sar"})()]

        def run(self, _, inputs):
            assert inputs["sar"].shape == (1, 2, 512, 512)
            assert inputs["sar"].dtype == np.float32
            # sigmoid(log(3)) = .75, not a thresholded binary mask.
            return [np.full((1, 1, 512, 512), np.log(3), np.float32)]

    monkeypatch.setattr(runtime, "_session", lambda _: Session())
    probability, valid = runtime.predict_sar_probability(path, "unused.onnx")
    assert probability.shape == shape
    if shape != (1, 1):
        assert not valid[0, 0]
        assert probability[0, 0] == 0
    np.testing.assert_allclose(probability[valid], .75)


def test_tile_edge_bias_does_not_make_a_grid(monkeypatch, tmp_path):
    path = tmp_path / "sar.tif"
    write_sar(path, np.full((2, 1024, 1024), -20.0))

    class EdgeBiasedSession:
        def get_inputs(self):
            return [type("Input", (), {"name": "sar"})()]

        def run(self, _, inputs):
            assert inputs["sar"].shape == (1, 2, 512, 512)
            logits = np.full((1, 1, 512, 512), np.log(9), np.float32)
            logits[:, :, :64, :] = -np.log(9)
            logits[:, :, -64:, :] = -np.log(9)
            logits[:, :, :, :64] = -np.log(9)
            logits[:, :, :, -64:] = -np.log(9)
            return [logits]

    monkeypatch.setattr(runtime, "_session", lambda _: EdgeBiasedSession())
    probability, valid = runtime.predict_sar_probability(path, "unused.onnx")
    assert valid.all()
    np.testing.assert_allclose(probability, .9, atol=1e-6)
