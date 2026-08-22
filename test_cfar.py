import rasterio
import numpy as np
import sys; sys.path.insert(0, "/app/app"); from cfar import cfar_detect

with rasterio.open('/tmp/sample.tif') as src:
    image = src.read(1).astype("float32")
    finite = np.isfinite(image)
    
    # CFAR
    anomaly_mask = cfar_detect(image, 3, 15, 2.5)
    
    print(f"CFAR pixels: {anomaly_mask.sum()}")
