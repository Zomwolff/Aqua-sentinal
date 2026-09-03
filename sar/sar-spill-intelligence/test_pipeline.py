import sys
sys.path.append("..")
sys.path.append("../lookalike-engine")
import asyncio
import numpy as np
import rasterio
from app.despeckle import lee_filter
from app.segmentation import dark_region_mask
from app.cfar import cfar_detect
from app.morphology import clean_mask
from app.polygonize import extract_candidates
from app.shape_filters import classify_candidate
from app.shape_filters import classify_candidate

async def test_full_pipeline(path):
    print(f"\n--- Testing {path} ---")
    with rasterio.open(path) as src:
        arr = src.read(1, masked=True)
        finite = np.isfinite(arr.filled(np.nan))
        fill_val = float(np.median(arr[finite]))
        working = np.where(finite, arr, fill_val)
        transform = src.transform
        
    print("Lee filtering...")
    filtered = lee_filter(working, 5, "linear")
    
    print("Masking...")
    dark = dark_region_mask(filtered)
    cfar = cfar_detect(filtered, 3, 15, 2.5)
    binary = (dark | cfar) & finite
    print(f"Binary mask pixels: {binary.sum()}")
    
    cleaned = clean_mask(binary, 3, 5)
    print(f"Cleaned mask pixels: {cleaned.sum()}")
    
    candidates = extract_candidates(cleaned, transform, 50000, 10.0)
    print(f"Extracted {len(candidates)} candidates.")
    
    bright_target = filtered > 5.0
    
    for i, cand in enumerate(candidates):
        mask_region = {
            "dark_mask": cleaned, 
            "intensity": filtered
        }
        label = classify_candidate(cand, mask_region, bright_target)
        print(f"  Candidate {i} (area_px={cand['pixel_count']}): {label}")

import glob
for p in glob.glob('../../tiff_data/sample_with_spill.tif'):
    asyncio.run(test_full_pipeline(p))
