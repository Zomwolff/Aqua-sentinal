import rasterio
import numpy as np

FILE_PATH = r"C:\Users\datha\Documents\GitHub\Aqua-sentinal\sar-LinkNet-ResNet34\dataset\mahi-ka-data\00012.tif"

with rasterio.open(FILE_PATH) as src:
    data = src.read(1)

    print(f"File: {FILE_PATH}")
    print(f"Shape: {data.shape}")
    print(f"Dtype: {data.dtype}")
    print(f"Min: {data.min()}")
    print(f"Max: {data.max()}")
    print(f"Mean: {data.mean():.6f}")
    print(f"Unique values (first 20): {np.unique(data)[:20]}")
    print(f"Number of unique values: {len(np.unique(data))}")
    print(f"Percent zero pixels: {100 * np.sum(data == 0) / data.size:.2f}%")