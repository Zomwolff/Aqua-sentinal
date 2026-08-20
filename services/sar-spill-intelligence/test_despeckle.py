import sys
from pathlib import Path

import numpy as np
import pytest

SERVICE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_ROOT))

from app.despeckle import lee_filter


def test_lee_filter_reduces_variance_without_mutating_input():
    rng = np.random.default_rng(20260820)
    image = 10.0 + rng.normal(0.0, 2.0, size=(96, 96))
    original = image.copy()

    filtered = lee_filter(image)

    assert filtered.shape == image.shape
    assert np.array_equal(image, original)
    assert np.var(filtered) < np.var(image)


def test_lee_filter_default_five_by_five_behavior():
    image = np.arange(81, dtype=np.float64).reshape(9, 9)

    assert np.array_equal(lee_filter(image), lee_filter(image, window_size=5))


@pytest.mark.parametrize("window_size", [0, 2, 4, -3])
def test_lee_filter_rejects_invalid_window_sizes(window_size):
    with pytest.raises(ValueError, match="positive odd integer"):
        lee_filter(np.ones((5, 5)), window_size=window_size)
