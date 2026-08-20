"""Generate a deterministic synthetic SAR GeoTIFF for E2E pipeline validation.

TEST-ONLY fixture. Produces a single-band VV-like raster that exercises the
Step 3 -> Step 4 pipeline using dark-region segmentation (Otsu) + CFAR union.

The values chosen here (bright-target backscatter, area thresholds) are fixture
parameters for the synthetic run ONLY. They are NOT scientifically validated
and must NOT be committed or reused as production defaults.

Usage:
    python3 scripts/generate_synthetic_sar_fixture.py [output_path]
"""
from __future__ import annotations

import sys

import numpy as np
import rasterio

SIZE = 512
ORIGIN = (72.75, 19.15)  # (lon, lat) top-left
RES = 0.0001  # deg/px (~11 m Sentinel-1 IW ground range)

BG_WATER = -1.0  # dominant dark-ish sea backscatter (tight, low variance)
BG_WATER_STD = 0.03

# Component definitions (fixture values only, not validated thresholds).
CALM_VALUE = -1.8  # large diffuse low-contrast dark region (calm water)
SHIP_STRIPE = -3.0  # elongated dark region next to a bright target
BRIGHT_VESSEL = 3.0
SLICK_RIM = -1.8  # compact dark blob with a darker core (high internal contrast)
SLICK_CORE = -4.0


def build_scene() -> np.ndarray:
    rng = np.random.default_rng(20240820)
    image = rng.normal(BG_WATER, BG_WATER_STD, size=(SIZE, SIZE))

    # Calm-water carpet: large, internally uniform, slightly darker than water.
    image[40:240, 40:240] = CALM_VALUE

    # Ship shadow: bright vessel with an elongated dark stripe immediately below.
    image[180:210, 260:380] = BRIGHT_VESSEL
    image[210:216, 260:380] = SHIP_STRIPE

    # Possible slick: compact blob with high internal contrast (dark rim/core).
    image[300:340, 300:340] = SLICK_RIM
    image[310:325, 310:325] = SLICK_CORE

    return image


def write_geotiff(path: str) -> str:
    image = build_scene()
    transform = rasterio.transform.from_origin(*ORIGIN, RES, RES)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=SIZE,
        width=SIZE,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as dataset:
        dataset.write(image.astype(np.float32), 1)
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "data/sample_sar/synthetic_e2e.tif"
    write_geotiff(out)
    print(f"wrote {out}")