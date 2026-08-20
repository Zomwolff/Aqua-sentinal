import sys
from pathlib import Path

import numpy as np
import pytest

SERVICE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_ROOT))

from app.texture import compute_glcm_features

FEATURE_KEYS = {
    "contrast",
    "homogeneity",
    "energy",
    "correlation",
    "mean_backscatter",
    "std_backscatter",
}


def _patch(rows=48, cols=48, r0=12, r1=36, c0=12, c1=36):
    raw = np.zeros((rows, cols), dtype=np.float64)
    region = np.zeros((rows, cols), dtype=bool)
    region[r0:r1, c0:c1] = True
    return raw, region


def test_returned_feature_keys():
    raw, region = _patch()
    raw[region] = np.random.default_rng(1).normal(-5.0, 1.0, region.sum())
    features = compute_glcm_features(raw, region, levels=32)
    assert set(features.keys()) == FEATURE_KEYS


def test_noisy_region_has_higher_contrast_than_uniform():
    raw_uniform, region = _patch()
    raw_uniform[region] = -5.0

    raw_noisy = raw_uniform.copy()
    raw_noisy[region] = np.random.default_rng(2).normal(-5.0, 1.5, region.sum())

    uniform = compute_glcm_features(raw_uniform, region, levels=32)
    noisy = compute_glcm_features(raw_noisy, region, levels=32)

    assert uniform["contrast"] == 0.0
    assert noisy["contrast"] > uniform["contrast"]
    assert noisy["energy"] <= uniform["energy"] + 1e-9


def test_raw_sar_values_drive_mean_and_std():
    raw, region = _patch()
    raw[region] = np.random.default_rng(3).normal(-10.0, 0.5, region.sum())
    features = compute_glcm_features(raw, region, levels=32)
    assert features["mean_backscatter"] == pytest.approx(-10.0, abs=0.2)
    assert features["std_backscatter"] == pytest.approx(0.5, abs=0.2)


def test_region_mask_is_respected():
    raw, region = _patch()
    raw[region] = -6.0  # candidate pixels only
    raw[~region] = 50.0  # non-candidate (out-of-region) clutter
    features = compute_glcm_features(raw, region, levels=32)
    # mean/std derive from the masked candidate values only, not the clutter.
    assert features["mean_backscatter"] == pytest.approx(-6.0, abs=1e-6)
    assert features["std_backscatter"] < 0.01
    assert features["contrast"] < 1e-6


def test_empty_region_returns_neutral_features():
    raw = np.zeros((16, 16))
    empty = np.zeros((16, 16), dtype=bool)
    features = compute_glcm_features(raw, empty, levels=32)
    assert features["contrast"] == 0.0
    assert features["energy"] == 1.0
    assert features["homogeneity"] == 1.0
    assert features["mean_backscatter"] == 0.0


def test_nonfinite_masked_values_do_not_crash():
    raw, region = _patch()
    raw[region] = np.random.default_rng(4).normal(-3.0, 0.8, region.sum())
    raw[region][0:5] = np.nan
    features = compute_glcm_features(raw, region, levels=32)
    assert np.isfinite(list(features.values())).all()


def test_quantization_levels_are_respected():
    raw, region = _patch()
    values = np.random.default_rng(5).uniform(-8.0, -2.0, region.sum())
    raw[region] = values
    f8 = compute_glcm_features(raw, region, levels=8)
    f32 = compute_glcm_features(raw, region, levels=32)
    assert set(f8.keys()) == FEATURE_KEYS == set(f32.keys())
    assert np.isfinite(list(f8.values())).all()


def test_invalid_levels_rejected():
    raw, region = _patch()
    with pytest.raises(ValueError):
        compute_glcm_features(raw, region, levels=1)


def test_shape_mismatch_rejected():
    raw = np.zeros((10, 10))
    mask = np.zeros((11, 11), dtype=bool)
    with pytest.raises(ValueError):
        compute_glcm_features(raw, mask)