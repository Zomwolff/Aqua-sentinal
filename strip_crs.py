import rasterio
import os

for i in range(1, 11):
    tmp_path = f"/tmp/raw_sample_{i}.tif"
    out_path = f"/tmp/sample_{i}.tif"
    if not os.path.exists(tmp_path):
        continue
    with rasterio.open(tmp_path) as src:
        meta = src.meta.copy()
        data = src.read(1)
    meta['crs'] = None
    meta['transform'] = rasterio.transform.Affine.identity()
    with rasterio.open(out_path, 'w', **meta) as dst:
        dst.write(data, 1)
print("Stripping complete.")
