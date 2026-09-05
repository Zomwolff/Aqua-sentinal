import sys
from pathlib import Path

import numpy as np
import pytest

SERVICE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_ROOT))

from app.cfar import cfar_detect


def _dark_target_scene():
    rng = np.random.default_rng(20260820)
    image = rng.normal(0.0, 0.25, size=(96, 96))
    rows, cols = np.ogrid[:96, :96]
    # Keep the planted region inside the 3x3 guard cells so it does not
    # contaminate the 15x15 background annulus being tested.
    dark_region = (rows - 48) ** 2 + (cols - 48) ** 2 <= 2
    image[dark_region] -= 3.0
    return image, dark_region


def test_cfar_detects_dark_region_more_often_than_background():
    image, dark_region = _dark_target_scene()

    mask = cfar_detect(image)
    target_detection_rate = mask[dark_region].mean()
    background_detection_rate = mask[~dark_region].mean()

    assert mask.dtype == np.bool_
    assert target_detection_rate >= 0.70
    assert background_detection_rate <= 0.05


def test_increasing_k_never_increases_dark_target_flags():
    image, _ = _dark_target_scene()

    flags_k2 = cfar_detect(image, k=2).sum()
    flags_k25 = cfar_detect(image, k=2.5).sum()
    flags_k3 = cfar_detect(image, k=3).sum()

    assert flags_k3 <= flags_k25 <= flags_k2


@pytest.mark.parametrize(
    ("guard_size", "background_size"),
    [(2, 15), (3, 14), (15, 3), (0, 15)],
)
def test_cfar_rejects_invalid_guard_and_background_windows(
    guard_size,
    background_size,
):
    with pytest.raises(ValueError):
        cfar_detect(
            np.zeros((31, 31)),
            guard_size=guard_size,
            background_size=background_size,
        )
