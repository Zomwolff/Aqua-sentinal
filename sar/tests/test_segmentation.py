import rasterio
import numpy as np
from skimage.filters import threshold_otsu

with rasterio.open('/tmp/sample.tif') as src:
    image = src.read(1).astype("float32")
    finite = np.isfinite(image)
    values = image[finite]
    median = np.median(values)
    lower = values[values <= median]
    threshold = threshold_otsu(lower)
    print(f"Median: {median:.2f}")
    print(f"Otsu threshold: {threshold:.2f}")
    print(f"Contrast (median - threshold): {median - threshold:.2f} dB")
