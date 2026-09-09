from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject


def align_sar_probability(sar_path: Path, eo_path: Path):
    """Reproject SAR probabilities and validity onto the exact EO grid."""
    with rasterio.open(sar_path) as sar, rasterio.open(eo_path) as eo:
        if sar.crs is None or eo.crs is None:
            raise ValueError("SAR and EO rasters must both have a CRS")
        source = sar.read(1).astype(np.float32)
        valid = sar.read_masks(1) > 0
        source[~valid] = -9999.0
        aligned = np.zeros((eo.height, eo.width), dtype=np.float32)
        aligned_valid = np.zeros((eo.height, eo.width), dtype=np.uint8)
        reproject(
            source, aligned,
            src_transform=sar.transform, src_crs=sar.crs, src_nodata=-9999.0,
            dst_transform=eo.transform, dst_crs=eo.crs, dst_nodata=0.0,
            resampling=Resampling.bilinear,
        )
        reproject(
            valid.astype(np.uint8), aligned_valid,
            src_transform=sar.transform, src_crs=sar.crs, src_nodata=0,
            dst_transform=eo.transform, dst_crs=eo.crs, dst_nodata=0,
            resampling=Resampling.nearest,
        )
        eo_valid = eo.read_masks(1) > 0
        valid_out = (aligned_valid > 0) & eo_valid
        aligned = np.clip(aligned, 0.0, 1.0).astype(np.float32)
        aligned[~valid_out] = 0.0
        return aligned, valid_out
