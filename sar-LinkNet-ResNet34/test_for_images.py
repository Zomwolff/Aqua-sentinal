# ============================================================
# SANITY CHECK: what does YOUR loading code actually see when
# given a real Sentinel-1 SAR .tif, vs. what your model expects
# from PNG training?
#
# Run this BEFORE debugging the model itself. This just confirms
# whether the input is being read correctly at all.
# ============================================================

import numpy as np

FILE_PATH = r"C:\Users\datha\Documents\GitHub\Aqua-sentinal\sar-LinkNet-ResNet34\dataset\mahi-ka-data\00012.tif"

print("=" * 60)
print("METHOD 1: How your PNG-trained pipeline probably loads it")
print("=" * 60)
try:
    from PIL import Image
    img = Image.open(FILE_PATH)
    arr = np.array(img)
    print(f"PIL loaded successfully")
    print(f"Mode: {img.mode}")
    print(f"Size: {img.size}")
    print(f"Array shape: {arr.shape}")
    print(f"Array dtype: {arr.dtype}")
    print(f"Min: {arr.min()}, Max: {arr.max()}, Mean: {arr.mean():.4f}")
except Exception as e:
    print(f"PIL FAILED to load this file: {type(e).__name__}: {e}")

print()
print("=" * 60)
print("METHOD 2: How it should actually be read (rasterio)")
print("=" * 60)
try:
    import rasterio
    with rasterio.open(FILE_PATH) as src:
        print(f"Bands: {src.count}")
        print(f"Size: {src.width} x {src.height}")
        print(f"Dtype: {src.dtypes}")
        data = src.read()
        print(f"Full array shape: {data.shape}")
        for band_index in range(src.count):
            band = data[band_index]
            print(f"  Band {band_index + 1}: min={band.min():.4f}, "
                  f"max={band.max():.4f}, mean={band.mean():.4f}")
except Exception as e:
    print(f"rasterio FAILED: {type(e).__name__}: {e}")

print()
print("=" * 60)
print("WHAT TO LOOK FOR")
print("=" * 60)
print("If METHOD 1 raised an error or gave a totally different shape/dtype")
print("than METHOD 2, that confirms your loading code can't handle this")
print("file correctly -- it needs to load with rasterio instead of PIL,")
print("and your model's expected input format (uint8 0-255, N channels,")
print("fixed patch size) needs an explicit conversion step from this")
print("raw float32 dB, 2-channel, 2048x2048 format before it reaches")
print("the model.")