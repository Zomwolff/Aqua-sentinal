import rasterio
import numpy as np
import os

source_path = "tiff_data/sample_1.tif"
dest_path = "tiff_data/sample_with_spill.tif"

with rasterio.open(source_path) as src:
    meta = src.meta.copy()
    img = src.read(1)

# Create a jagged, dark polygon in the center to simulate an oil slick
height, width = img.shape
cy, cx = height // 2, width // 2

# Manually create a mask instead of using skimage (since it's not installed)
rr, cc = np.ogrid[:height, :width]
dist = ((rr - cy)**2 + ((cc - cx)/3)**2) ** 0.5
mask = (dist < 300) & (dist > 50) & ((rr + cc) % 20 < 15)

# Darken those pixels (oil spills reduce backscatter significantly)
# Add some random noise so the standard deviation doesn't drop too low,
# which would cause the calm-water heuristic to reject it.
noise = np.random.normal(0, 2.0, img[mask].shape)
img[mask] = img[mask] - 10.0 + noise

with rasterio.open(dest_path, 'w', **meta) as dst:
    dst.write(img, 1)

print("Created sample_with_spill.tif")
