"""Post-processing: model mask -> cleaned mask -> polygons -> GIS-ready spill boundary.

Plugs in AFTER infer_pipeline.py. Takes the `{name}_mask.png` it wrote, plus
the ORIGINAL source image (needed to recover geographic reference), and
produces:

    {name}_mask_clean.png     - morphologically cleaned binary mask
    {name}_spill.geojson      - polygon(s) of the spill boundary
    {name}_spill.shp (+ .dbf/.shx/.prj) - same polygons as a shapefile
    {name}_spill_meta.json    - CRS/area info + per-candidate GLCM texture

Georeferencing behaviour (important, read this):
  - If --source-image is a GeoTIFF with an embedded CRS + affine transform
    (check with `gdalinfo your.tif` or `rasterio.open(...).crs`), this script
    reads that transform and reprojects every polygon vertex from pixel
    space into that CRS, then reprojects to EPSG:4326 (lat/lon) for the
    GeoJSON and keeps the native CRS for the shapefile (both are written
    with their real CRS attached, no guessing).
  - If --source-image has NO CRS (plain PNG/JPG, or a TIFF with no geo tags),
    there is no way to recover real-world coordinates from pixels alone.
    In that case this script REFUSES to silently fabricate coordinates: it
    writes the polygons in raw pixel coordinates, tags the output files
    clearly as "PIXEL_SPACE_NOT_GEOREFERENCED", and prints a loud warning.
    If you know the real-world corner coordinates of this tile (e.g. from
    whatever cut it out of a larger scene), pass them with
    --manual-bounds "min_lon,min_lat,max_lon,max_lat" and it will
    georeference properly from that instead.

POST-MODEL GLCM texture (Haralick features):
  - After the cleaned + NoData-filtered candidate mask is final, each
    connected component (candidate) gets its own masked GLCM computed from
    the ORIGINAL SAR raster (--source-image), NOT from the model mask.
  - The 2D spatial arrangement is preserved: SAR crop + 2D candidate mask
    per bounding box; only pixel pairs where BOTH pixels are inside the
    candidate AND valid SAR contribute to the co-occurrence counts.
  - SAR dB values are clipped to a fixed configurable range (default
    [-30, 0] dB), normalized to 0..1, then uniformly quantized to 32 levels.
    This normalization is INDEPENDENT of the neural-network preprocessing in
    infer_pipeline.py (ImageNet mean/std) -- do not confuse the two.
  - Texture is descriptive evidence only (a future B2 Random Forest input).
    This script applies NO "high homogeneity = oil" style thresholds.

Usage:
    python geo_postprocess.py \
        --mask outputs/demo/tile_mask.png \
        --source-image your_images/tile.tif \
        --output-dir outputs/demo

    # tile with no embedded CRS but known corner coords:
    python geo_postprocess.py \
        --mask outputs/demo/tile_mask.png \
        --source-image your_images/tile.png \
        --manual-bounds "72.80,18.90,72.85,18.95" \
        --output-dir outputs/demo

    # custom GLCM configuration (band + range are explicit and configurable;
    # defaults are NOT scientifically validated -- choose them from the
    # Sentinel-1 export recipe: confirm band/polarization, value representation,
    # then the global dB range):
    python geo_postprocess.py \
        --mask outputs/demo/tile_mask.png \
        --source-image your_images/tile.tif \
        --output-dir outputs/demo \
        --glcm-band 1 --glcm-levels 32 --glcm-min-db -30 --glcm-max-db 0 \
        --glcm-distances 1 2 --glcm-angles 0 45 90 135
"""

import argparse
import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image

try:
    import rasterio
    from rasterio.features import shapes as rio_shapes
    from rasterio.transform import from_bounds
    from rasterio.warp import transform_geom
    from rasterio.crs import CRS
except ImportError as e:
    raise SystemExit("Missing dependency. Run: pip install rasterio shapely geopandas scikit-image scipy") from e

import geopandas as gpd
from shapely.geometry import shape as shp_shape
from scipy import ndimage as ndi
from skimage.morphology import remove_small_objects, remove_small_holes, opening, closing, disk, dilation, erosion
from skimage.measure import regionprops

try:
    from pyproj import Geod as _Geod
    _HAS_GEOD = True
except ImportError:  # pragma: no cover
    _HAS_GEOD = False

try:
    from skimage.feature import graycomatrix, graycoprops  # noqa: F401
    _HAS_SKIMAGE_GLCMS = True
except ImportError:  # pragma: no cover - handled at texture-extraction time
    _HAS_SKIMAGE_GLCMS = False


# --------------------------------------------------------------------------
# NoData handling (critical for Sentinel-1 / GEE exports)
# --------------------------------------------------------------------------

def get_valid_data_mask(source_image: Path, shape_hw: tuple) -> np.ndarray | None:
    """Return a boolean (H, W) mask of pixels that are real data (not nodata).

    Why this matters: Sentinel-1 swaths are NOT axis-aligned. GEE exports a
    rectangular bounding box, so the area outside the actual swath is filled
    with nodata (commonly 0 or a large negative sentinel like -9999). Those
    regions are typically dark/flat, which is exactly what an oil-spill
    segmentation model tends to fire on -- producing a false "spill" polygon
    shaped like the swath's padding wedge/border. Without this mask, that
    false polygon gets written straight into the shapefile.

    Returns None if the source can't be read or has no nodata info, in which
    case the caller must proceed without nodata filtering (and should say so).
    """
    try:
        with rasterio.open(source_image) as src:
            if (src.height, src.width) != shape_hw:
                return None  # size mismatch handled/warned by the caller already
            data = src.read()  # (bands, H, W)
            if src.nodata is not None:
                if np.isnan(src.nodata):
                    valid = ~np.any(np.isnan(data), axis=0)
                else:
                    valid = ~np.any(data == src.nodata, axis=0)
            elif src.nodatavals and any(v is not None for v in src.nodatavals):
                valid = np.ones(shape_hw, dtype=bool)
                for band_idx, nd in enumerate(src.nodatavals):
                    if nd is not None:
                        valid &= data[band_idx] != nd
            elif src.count >= 4 or src.colorinterp and rasterio.enums.ColorInterp.alpha in src.colorinterp:
                # Explicit alpha band present.
                alpha_idx = [i for i, ci in enumerate(src.colorinterp) if ci == rasterio.enums.ColorInterp.alpha]
                if alpha_idx:
                    valid = data[alpha_idx[0]] > 0
                else:
                    return None
            else:
                # No declared nodata at all -- do NOT guess (e.g. don't assume 0
                # means nodata, since 0 can be a legitimate dB/backscatter value).
                return None
            return valid
    except rasterio.errors.RasterioIOError:
        return None


# --------------------------------------------------------------------------
# Morphological cleanup
# --------------------------------------------------------------------------

def clean_mask(mask: np.ndarray, min_object_px: int = 20, min_hole_px: int = 64,
               opening_radius: int = 0, closing_radius: int = 1) -> np.ndarray:
    """Speckle removal + hole filling + smoothing on a binary mask.

    Order matters:
      1. Optional opening (erode-then-dilate) strips isolated speckle noise.
         It is OFF by default because oil slicks can be narrow.
      2. Closing (dilate-then-erode) fills small gaps/holes and smooths
         jagged edges from the raw sigmoid threshold.
      3. remove_small_objects drops any remaining blobs below min_object_px
         (these are almost always false positives, not real spill fragments).
      4. remove_small_holes fills tiny interior holes below min_hole_px
         (avoids donut-shaped polygons from noisy interior pixels).
    """
    m = mask.astype(bool)
    # Opening is intentionally OFF by default. A disk opening first erodes
    # the mask, which can completely erase the narrow elongated streaks that
    # are common in SAR oil-spill predictions.
    if opening_radius > 0:
        m = opening(m, disk(opening_radius))

    if closing_radius > 0:
        m = closing(m, disk(closing_radius))

    if min_object_px > 1:
        m = remove_small_objects(m, min_size=min_object_px)

    # scikit-image 0.26+ deprecates area_threshold in favor of max_size.
    # max_size removes/fills holes whose area is <= max_size.
    if min_hole_px > 0:
        m = remove_small_holes(m, max_size=min_hole_px)

    return m


# --------------------------------------------------------------------------
# Georeferencing
# --------------------------------------------------------------------------

def get_source_transform_and_crs(source_image: Path, manual_bounds: str | None, shape_hw: tuple):
    """Return (transform, crs, is_georeferenced: bool).

    Priority: 1) --manual-bounds if given, 2) embedded CRS/transform in the
    source raster (rasterio), 3) None/None -> caller must fall back to pixel space.
    """
    h, w = shape_hw
    if manual_bounds:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(x) for x in manual_bounds.split(","))
        except ValueError:
            raise ValueError('--manual-bounds must be "min_lon,min_lat,max_lon,max_lat"')
        transform = from_bounds(min_lon, min_lat, max_lon, max_lat, w, h)
        return transform, CRS.from_epsg(4326), True

    try:
        with rasterio.open(source_image) as src:
            if src.crs is not None and src.transform is not None and src.transform != rasterio.Affine.identity():
                if (src.height, src.width) != (h, w):
                    warnings.warn(
                        f"Source raster size {(src.height, src.width)} != mask size {(h, w)}; "
                        "georeferencing will be approximate. Re-run infer_pipeline on the exact "
                        "same file this mask was produced from."
                    )
                return src.transform, src.crs, True
    except rasterio.errors.RasterioIOError:
        pass  # not a raster rasterio can open (e.g. plain PNG); fall through

    return rasterio.transform.IDENTITY, None, False


# --------------------------------------------------------------------------
# Polygon extraction
# --------------------------------------------------------------------------

def mask_to_polygons(mask: np.ndarray, transform) -> list[dict]:
    """Vectorize a binary mask into polygon geometries (in transform's space)."""
    mask_u8 = mask.astype(np.uint8)
    polygons = []
    for geom, value in rio_shapes(mask_u8, mask=mask_u8.astype(bool), transform=transform):
        if value == 1:
            polygons.append(geom)
    return polygons


# --------------------------------------------------------------------------
# POST-MODEL GLCM / Haralick texture
# --------------------------------------------------------------------------
# Pipeline per candidate:
#   SAR dB --clip--> fixed range --normalize--> 0..1 --quantize--> 0..31
#   --masked pair counting--> per-(distance, angle) GLCM --Haralick--> mean/std
#
# NORMALIZATION vs QUANTIZATION (do not confuse):
#   normalization: SAR dB  -> 0..1  (continuous, fixed physical range)
#   quantization:  0..1    -> 0..31 (discrete gray levels for GLCM input)
#
# This is INDEPENDENT of infer_pipeline.py model preprocessing
# (uint8 -> /255 -> ImageNet mean/std for LinkNet). That normalization feeds
# the neural network; this one feeds GLCM. Keep them separate.
#
# SYMMETRY: with symmetric=True, encountering pair (i, j) at an offset also
# accumulates (j, i), i.e. counts are mirrored (P += P.T) before normalizing.
# This makes the GLCM direction-independent (equivalent to counting each
# neighbour pair in both orders), matching skimage's symmetric=True.
#
# SCIENTIFIC LIMITATION: these are descriptive texture features for a future
# B2 Random Forest (combined with backscatter, shape, area, wind, AIS,
# temporal persistence, ...). NEVER threshold them here into oil/look-alike
# decisions -- oil slick SAR appearance depends on wind, waves, incidence
# angle, polarization, age, oil type, processing and look-alikes.

GLCM_DEFAULT_LEVELS = 32
# Legacy manual dB fallback (kept for direct extract_candidate_textures callers).
# The run_postprocess CLI defaults to None = auto scene p1/p99 range instead.
GLCM_DEFAULT_MIN_DB = -30.0
GLCM_DEFAULT_MAX_DB = 0.0
GLCM_DEFAULT_DISTANCES = [1, 2]
GLCM_DEFAULT_ANGLES_DEG = [0, 45, 90, 135]
GLCM_DEFAULT_SYMMETRIC = True
GLCM_DEFAULT_MIN_TEXTURE_PIXELS = 20
# Default texture band: raster band 1. This is a CONFIGURABLE default, not a
# scientifically validated polarization choice -- the TIFF metadata inspected
# so far does not identify VV/VH, so the user must select the intended
# Sentinel-1 band via --glcm-band based on the export recipe.
GLCM_DEFAULT_BAND = 1


def _glcm_offsets(distances, angles_deg):
    """Integer (row, col) neighbour offsets for each (distance, angle).

    Convention matches skimage.feature.graycomatrix:
      0 deg   -> (0, +d)    horizontal
      45 deg  -> (-d, +d)   diagonal up-right
      90 deg  -> (-d, 0)    vertical
      135 deg -> (-d, -d)   diagonal up-left
    """
    offsets = []
    for d in distances:
        d = int(d)
        for a in angles_deg:
            r = float(a) % 180.0
            if r == 0:
                dr, dc = 0, d
            elif r == 45:
                dr, dc = -d, d
            elif r == 90:
                dr, dc = -d, 0
            elif r == 135:
                dr, dc = -d, -d
            else:
                rad = np.deg2rad(float(a))
                dr = int(round(-d * np.sin(rad)))
                dc = int(round(d * np.cos(rad)))
            offsets.append({"distance": d, "angle_deg": int(a) if float(a).is_integer() else float(a),
                            "dr": dr, "dc": dc})
    return offsets


def _quantize_sar(sar_values: np.ndarray, min_db: float, max_db: float, levels: int) -> np.ndarray:
    """Clip SAR dB to [min_db, max_db] -> normalize to 0..1 -> quantize to 0..levels-1.

    Non-finite inputs (NaN/Inf) are pinned to min_db BEFORE quantization so the
    cast never sees NaN (they are excluded from GLCM pair counting anyway via
    the valid-data mask -- this just keeps the quantized array well-defined).
    """
    arr = np.nan_to_num(sar_values.astype(np.float64), nan=min_db,
                        posinf=max_db, neginf=min_db)
    clipped = np.clip(arr, min_db, max_db)
    normalized = (clipped - min_db) / (max_db - min_db)
    quantized = np.floor(normalized * (levels - 1)).astype(np.int32)
    np.clip(quantized, 0, levels - 1, out=quantized)
    return quantized


def _haralick_from_glcm(p: np.ndarray) -> dict:
    """Haralick features from a NORMALIZED (probabilities sum to 1) GLCM.

    Definitions match skimage.feature.graycoprops for contrast, homogeneity
    (a.k.a. inverse difference moment), energy (sqrt of ASM) and correlation:
      contrast     = sum P(i,j) * (i-j)^2
      homogeneity  = sum P(i,j) / (1 + |i-j|)
      ASM          = sum P(i,j)^2 ; energy = sqrt(ASM)
      correlation  = sum (i-mu_x)(j-mu_y) P(i,j) / (sigma_x sigma_y)
    Zero-variance convention: if sigma_x or sigma_y is 0 (e.g. a constant
    region concentrates all mass in one GLCM cell), correlation is defined
    as 1.0 when the single occupied cell lies on the diagonal (perfect
    self-similarity) and 0.0 otherwise. This avoids NaN while remaining
    documented and deterministic.
    """
    levels = p.shape[0]
    i, j = np.mgrid[0:levels, 0:levels].astype(np.float64)
    contrast = float(np.sum(p * (i - j) ** 2))
    homogeneity = float(np.sum(p / (1.0 + np.abs(i - j))))
    asm = float(np.sum(p ** 2))
    energy = float(np.sqrt(asm))
    mu_x = float(np.sum(i * p))
    mu_y = float(np.sum(j * p))
    var_x = float(np.sum(((i - mu_x) ** 2) * p))
    var_y = float(np.sum(((j - mu_y) ** 2) * p))
    sx, sy = np.sqrt(var_x), np.sqrt(var_y)
    if sx == 0.0 or sy == 0.0:
        diag_mass = float(np.trace(p))
        correlation = 1.0 if diag_mass >= 1.0 - 1e-12 else 0.0
    else:
        correlation = float(np.sum((i - mu_x) * (j - mu_y) * p) / (sx * sy))
        correlation = float(np.clip(correlation, -1.0, 1.0))
    out = {"contrast": contrast, "homogeneity": homogeneity,
           "energy": energy, "asm": asm, "correlation": correlation}
    # Guarantee no NaN/Inf ever leaks into the metadata.
    return {k: (float(v) if np.isfinite(v) else 0.0) for k, v in out.items()}


def _masked_pair_counts(quant_crop: np.ndarray, valid_crop: np.ndarray,
                        levels: int, offsets: list, symmetric: bool = True):
    """Count gray-level pairs with a 2D mask-aware approach.

    For each offset, only pairs where BOTH pixels are valid (inside the
    candidate AND valid SAR) contribute. Background / NoData pixels never
    create artificial co-occurrences. Returns (glcms, pair_counts) where
    glcms[k] is the normalized levels x levels matrix for offsets[k].
    """
    h, w = quant_crop.shape
    glcms, pair_counts = [], []
    for off in offsets:
        dr, dc = off["dr"], off["dc"]
        # Overlapping source/target windows for this offset (vectorized, no
        # per-pixel Python loop).
        r0 = max(0, -dr); r1 = h - max(0, dr)
        c0 = max(0, -dc); c1 = w - max(0, dc)
        t0 = max(0, dr); t1 = h - max(0, -dr)
        s0 = max(0, dc); s1 = w - max(0, -dc)
        counts = np.zeros((levels, levels), dtype=np.float64)
        n_pairs = 0
        if r1 > r0 and c1 > c0:
            src_v = valid_crop[r0:r1, c0:c1]
            dst_v = valid_crop[t0:t1, s0:s1]
            both = src_v & dst_v
            n = int(both.sum())
            if n > 0:
                a = quant_crop[r0:r1, c0:c1][both].ravel()
                b = quant_crop[t0:t1, s0:s1][both].ravel()
                idx = a.astype(np.int64) * levels + b.astype(np.int64)
                flat = np.bincount(idx, minlength=levels * levels).astype(np.float64)
                counts = flat.reshape(levels, levels)
                if symmetric:
                    # Count (i,j) and (j,i): direction-independent GLCM.
                    counts = counts + counts.T
                    n_pairs = int(2 * n)
                else:
                    n_pairs = n
        total = counts.sum()
        prob = counts / total if total > 0 else counts
        glcms.append(prob)
        pair_counts.append(n_pairs)
    return glcms, pair_counts


def _detect_polarization(src, band_idx: int) -> str:
    """Return the band's polarization ONLY if the raster metadata states it.

    Inspects band descriptions plus dataset/band tags for explicit tokens
    (VV/VH/HH/HV/polarization). Returns "unknown" otherwise -- never guessed
    from value statistics, because backscatter level alone cannot prove
    polarization.
    """
    haystacks = []
    try:
        desc = src.descriptions[band_idx - 1] if src.descriptions else None
        if desc:
            haystacks.append(str(desc))
    except Exception:
        pass
    try:
        for v in list(src.tags(band_idx).values()) + list(src.tags().values()):
            haystacks.append(str(v))
    except Exception:
        pass
    blob = " ".join(haystacks).upper()
    for pol in ("VV", "VH", "HH", "HV"):
        if pol in blob:
            return pol
    return "unknown"


def sar_band_stats(values: np.ndarray) -> dict:
    """Diagnostic statistics for the SELECTED SAR band (values never modified)."""
    f = np.asarray(values, dtype=np.float64).ravel()
    f = f[np.isfinite(f)]
    if f.size == 0:
        return {"count_finite": 0}
    return {
        "count_finite": int(f.size),
        "min": float(f.min()), "max": float(f.max()),
        "mean": float(f.mean()), "median": float(np.median(f)),
        "std": float(f.std()),
        "p01": float(np.percentile(f, 1)), "p05": float(np.percentile(f, 5)),
        "p95": float(np.percentile(f, 95)), "p99": float(np.percentile(f, 99)),
    }


def resolve_glcm_range(sar: np.ndarray, min_db, max_db):
    """Resolve the fixed GLCM normalization range for a scene.

    Explicit values are honored verbatim (source "manual"). When either bound
    is None (the default), the range is auto-fit from SCENE-level percentiles
    (p1/p99 of finite band values) so heterogeneous products each get a
    window covering their actual dynamic range. The resolved range still
    applies identically to every candidate in the scene (fixed-range
    normalization -- never per-candidate). Degenerate constant scenes fall
    back to [value-1, value+1] rather than crashing.

    Returns (min_db, max_db, range_source).
    """
    if min_db is not None and max_db is not None:
        if float(min_db) >= float(max_db):
            raise ValueError(f"--glcm-min-db ({min_db}) must be < --glcm-max-db ({max_db})")
        return float(min_db), float(max_db), "manual"
    f = np.asarray(sar, dtype=np.float64).ravel()
    f = f[np.isfinite(f)]
    if f.size == 0:
        raise ValueError("no finite SAR pixels to derive GLCM range from")
    auto_min = float(np.percentile(f, 1)) if min_db is None else float(min_db)
    auto_max = float(np.percentile(f, 99)) if max_db is None else float(max_db)
    if not auto_max > auto_min:
        mid = float(auto_min)
        auto_min, auto_max = mid - 1.0, mid + 1.0
    if min_db is not None and not auto_max > float(min_db):
        raise ValueError(f"--glcm-min-db ({min_db}) must be < derived max ({auto_max})")
    if max_db is not None and not float(max_db) > auto_min:
        raise ValueError(f"derived min ({auto_min}) must be < --glcm-max-db ({max_db})")
    return auto_min, auto_max, "auto_p01_p99"


# --------------------------------------------------------------------------
# B2 Look-Alike Classifier — candidate feature helpers
# --------------------------------------------------------------------------
# Every helper below returns real measured values or None (with a reason);
# NOTHING is fabricated. Missing contextual evidence (no weather/AIS/other
# scenes) stays missing (null) so the Random Forest can treat it as absent.
#
# Formulas:
#   mean/std_backscatter : mean/std of raw SAR dB values inside the candidate
#                          mask, finite values only, NoData excluded.
#   perimeter_m          : metric perimeter of the candidate polygon. Projected
#                          CRS -> planar length (metre units verified, else null).
#                          Geographic CRS -> geodesic length via pyproj.Geod.
#                          NEVER degrees reported as meters.
#   elongation           : regionprops major_axis_length / minor_axis_length on
#                          the candidate mask (same approach as
#                          services/lookalike-engine/app/shape_filters.py),
#                          capped at 1e6 for degenerate (near-zero minor axis).
#   boundary_irregularity: perimeter_m^2 / (4*pi*area_m2); ~1 for a circle.
#                          perimeter and area must both be metric.
#   edge_sharpness       : mean SAR outside the boundary band minus mean SAR
#                          inside the boundary band (dilated/eroded disk(3)
#                          bands, finite values only). Positive = backscatter
#                          drops going inside (dark-region edge).
#   wind_speed_kmh       : matched 10-m wind from weather records (nearest in
#                          space among samples within +-window of acquisition).
#   distance_to_nearest_vessel_km: min geodesic distance from candidate
#                          centroid to correlated AIS positions (km).
#   persistence_count    : detections of the same region across distinct SAR
#                          scenes INCLUDING the current scene (1 = only here).

ELONGATION_CAP = 1e6
_PX_EPS = float(np.finfo(np.float64).eps)

# Short Shapefile (<=10 char) aliases for the B2 scalar attributes. GeoJSON
# keeps the full names; this map is stored in spill_meta.json as shp_field_map.
SHP_FIELD_MAP = {
    "candidate_id": "cand_id",
    "area_m2": "area_m2",
    "perimeter_m": "perim_m",
    "elongation": "elong",
    "boundary_irregularity": "bnd_irreg",
    "edge_sharpness": "edge_sharp",
    "mean_backscatter": "mean_bs",
    "std_backscatter": "std_bs",
    "glcm_contrast": "glcm_con",
    "glcm_homogeneity": "glcm_hom",
    "glcm_energy": "glcm_en",
    "glcm_correlation": "glcm_corr",
    "wind_speed_kmh": "wind_kmh",
    "distance_to_nearest_vessel_km": "ves_km",
    "persistence_count": "persist_n",
}

B2_FEATURE_COLUMNS = [
    "mean_backscatter", "std_backscatter",
    "glcm_contrast", "glcm_homogeneity", "glcm_energy", "glcm_correlation",
    "area_km2", "perimeter_m", "elongation", "boundary_irregularity",
    "edge_sharpness", "wind_speed_kmh", "distance_to_nearest_vessel_km",
    "persistence_count",
]

B2_META_COLUMNS = [
    "candidate_id", "scene_id", "source_id", "label", "label_name",
    "category", "scene_datetime", "centroid_lat", "centroid_lon",
]


def _backscatter_stats(sar_crop: np.ndarray, valid_mask: np.ndarray):
    """Mean/std of raw SAR dB inside the candidate (finite valid pixels only)."""
    vals = np.asarray(sar_crop, dtype=np.float64)[np.asarray(valid_mask, dtype=bool)]
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return None, None
    return float(np.mean(vals)), float(np.std(vals))


def _edge_sharpness(sar_crop: np.ndarray, cand_mask: np.ndarray):
    """Outside-band mean minus inside-band mean SAR (disk(3) boundary bands).

    Returns (value, reason); value is None when either band has no finite
    SAR pixel -- reported as missing, never invented.
    """
    dark = np.asarray(cand_mask, dtype=bool)
    arr = np.asarray(sar_crop, dtype=np.float64)
    try:
        dilated = dilation(dark, footprint=disk(3))
        eroded = erosion(dark, footprint=disk(3))
    except Exception as e:
        return None, f"morphology_failed: {e}"
    outer = arr[dilated & ~dark]
    inner = arr[dark & ~eroded]
    outer = outer[np.isfinite(outer)]
    inner = inner[np.isfinite(inner)]
    if outer.size == 0 or inner.size == 0:
        return None, "insufficient_boundary_pixels"
    return float(np.mean(outer) - np.mean(inner)), "ok"


def _elongation(cand_mask: np.ndarray):
    """Major/minor axis ratio via regionprops (capped at 1e6 when degenerate)."""
    try:
        props = regionprops(np.asarray(cand_mask, dtype=np.int32))
    except Exception:
        return None
    if not props:
        return None
    region = max(props, key=lambda p: p.area)
    try:
        minor = float(region.axis_minor_length)
        major = float(region.axis_major_length)
    except Exception:
        return None
    if not (np.isfinite(minor) and np.isfinite(major)):
        return None
    if minor <= _PX_EPS:
        return float(ELONGATION_CAP)
    return float(major / minor)


def _geod():
    if not _HAS_GEOD:
        raise SystemExit("Missing dependency for metric geometry. Run: pip install pyproj")
    return _Geod(ellps="WGS84")


def _candidate_polygon_metrics(cand_bool_full: np.ndarray, transform, crs, is_geo):
    """Metric perimeter (+native polygon) for ONE candidate mask.

    Vectorizes the single-candidate mask with the source transform, then:
      projected CRS with metre units -> planar length (meters);
      geographic CRS                  -> geodesic length via pyproj.Geod;
      pixel space                     -> perimeter_m None (never fabricated).
    Returns dict with keys: polygon_native (shapely|None), polygon_wgs84
    (GeoJSON-dict|None), perimeter_m (float|None), perimeter_status (str).
    """
    out = {"polygon_native": None, "polygon_wgs84": None,
           "perimeter_m": None, "perimeter_status": "not_computed"}
    try:
        geoms = [g for g, v in rio_shapes(cand_bool_full.astype(np.uint8),
                                          mask=cand_bool_full.astype(bool),
                                          transform=transform) if v == 1]
    except Exception as e:
        out["perimeter_status"] = f"vectorize_failed: {e}"
        return out
    if not geoms:
        out["perimeter_status"] = "empty_geometry"
        return out
    try:
        from shapely.geometry import shape as _shape
        polys = [_shape(g) for g in geoms]
        poly = max(polys, key=lambda p: p.area)
    except Exception as e:
        out["perimeter_status"] = f"geometry_failed: {e}"
        return out
    out["polygon_native"] = poly
    if not is_geo or crs is None:
        out["perimeter_status"] = "pixel_space_no_metric"
        return out
    try:
        if crs.is_projected:
            unit = ""
            try:
                unit = (crs.axis_info[0].unit_name or "").lower()
            except Exception:
                unit = ""
            if unit and unit != "metre":
                out["perimeter_status"] = f"projected_non_metre_unit:{unit}"
                return out
            out["perimeter_m"] = float(poly.length)
            out["perimeter_status"] = "ok_projected_metres"
        else:
            g = _geod()
            total = g.geometry_length(poly)
            out["perimeter_m"] = float(total)
            out["perimeter_status"] = "ok_geodesic"
        wgs = transform_geom(crs, "EPSG:4326", shp_shape(poly.__geo_interface__).__geo_interface__)
        out["polygon_wgs84"] = wgs
    except Exception as e:
        out["perimeter_m"] = None
        out["perimeter_status"] = f"metric_failed: {e}"
    return out


def _boundary_irregularity(perimeter_m, area_m2):
    """perimeter^2 / (4*pi*area); ~1 for a circle. None when not computable."""
    try:
        if perimeter_m is None or area_m2 is None:
            return None
        p, a = float(perimeter_m), float(area_m2)
        if not (np.isfinite(p) and np.isfinite(a)) or a <= 0 or p < 0:
            return None
        return float(p * p / (4.0 * np.pi * a))
    except Exception:
        return None


def _parse_time(value):
    if value is None:
        return None
    try:
        from datetime import datetime
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _centroid_lonlat(polygon_wgs84, bbox_xyxy, transform):
    """Candidate centroid as (lon, lat); falls back to bbox center via transform."""
    try:
        if polygon_wgs84 is not None:
            from shapely.geometry import shape as _shape
            c = _shape(polygon_wgs84).representative_point()
            return float(c.x), float(c.y)
    except Exception:
        pass
    try:
        xmin, ymin, xmax, ymax = bbox_xyxy
        cx = (xmin + xmax + 1) / 2.0
        cy = (ymin + ymax + 1) / 2.0
        from rasterio.transform import xy as _xy
        lon, lat = _xy(transform, cy, cx, offset="center")
        return float(lon), float(lat)
    except Exception:
        return None


def _match_wind(centroid_lonlat, acquisition_time, records, window_h=3.0):
    """Nearest weather sample in space among records within +-window_h of acquisition.

    records: list of dicts with lat, lon, timestamp (ISO), wind_speed_kmh, source.
    Mirrors the existing evidence-fusion lookup (time window, then nearest).
    Returns (value|None, matched_dict|None, reason).
    """
    if not records:
        return None, None, "no_weather_source"
    if centroid_lonlat is None:
        return None, None, "no_centroid"
    if acquisition_time is None:
        return None, None, "no_acquisition_time"
    try:
        import math
        lon0, lat0 = centroid_lonlat
        best, best_d2 = None, None
        for r in records:
            try:
                ts = _parse_time(r.get("timestamp"))
                if ts is None:
                    continue
                if abs((ts - acquisition_time).total_seconds()) > float(window_h) * 3600.0:
                    continue
                d2 = (float(r["lat"]) - lat0) ** 2 + (float(r["lon"]) - lon0) ** 2
                if best is None or d2 < best_d2:
                    best, best_d2 = r, d2
            except Exception:
                continue
        if best is None:
            return None, None, "no_sample_in_window"
        val = best.get("wind_speed_kmh")
        if val is None or not np.isfinite(float(val)):
            return None, None, "matched_sample_missing_wind"
        matched = {"wind_speed_kmh": float(val),
                   "timestamp": best.get("timestamp"),
                   "source": best.get("source", "weather_csv"),
                   "distance_deg": float(math.sqrt(best_d2))}
        return float(val), matched, "ok"
    except Exception as e:
        return None, None, f"wind_match_failed: {e}"


def _nearest_vessel_km(centroid_lonlat, acquisition_time, records, window_h=24.0):
    """Min geodesic distance (km) from candidate centroid to AIS positions.

    records: list of dicts with lat, lon, optional timestamp/mmsi. Time filter
    applies only when BOTH acquisition and record timestamps exist (mirrors
    the existing correlation-window pattern). Returns
    (km|None, detail|None, reason); None (not 0) when nothing correlates.
    """
    if not records:
        return None, None, "no_ais_source"
    if centroid_lonlat is None:
        return None, None, "no_centroid"
    try:
        g = _geod()
        lon0, lat0 = centroid_lonlat
        best_m, best_rec = None, None
        for r in records:
            try:
                rts = _parse_time(r.get("timestamp"))
                if rts is not None and acquisition_time is not None:
                    if abs((rts - acquisition_time).total_seconds()) > float(window_h) * 3600.0:
                        continue
                _, _, dist_m = g.inv(lon0, lat0, float(r["lon"]), float(r["lat"]))
                if best_m is None or dist_m < best_m:
                    best_m, best_rec = float(dist_m), r
            except Exception:
                continue
        if best_m is None or best_rec is None:
            return None, None, "no_vessel_in_window"
        detail = {"distance_m": float(best_m), "distance_km": float(best_m / 1000.0),
                  "mmsi": best_rec.get("mmsi"), "timestamp": best_rec.get("timestamp")}
        return float(best_m / 1000.0), detail, "ok"
    except Exception as e:
        return None, None, f"vessel_match_failed: {e}"


def _geod_area(geom_wgs84):
    """Geodesic area (m^2, always >= 0) of a lon/lat GeoJSON geometry."""
    from shapely.geometry import shape as _shape
    return abs(float(_geod().geometry_area_perimeter(_shape(geom_wgs84))[0]))


def _persistence_count(polygon_wgs84, scene_id, acquisition_time,
                       other_scenes, iou_thresh=0.1, window_h=720.0):
    """Scene-to-scene match count INCLUDING the current scene (min 1 if valid).

    other_scenes: list of {scene_id, acquisition_time (ISO|None),
    polygons_wgs84 (list of GeoJSON dicts)}. A scene matches when any of its
    polygons has IoU >= iou_thresh with this candidate (IoU from geodesic
    areas) AND (when both timestamps exist) |dt| <= window_h. Unique scenes
    counted once; the current scene always counts itself.
    Returns (count, matched_scene_ids, reason).
    """
    if polygon_wgs84 is None:
        return None, [], "no_geometry"
    try:
        from shapely.geometry import shape as _shape
        mine = _shape(polygon_wgs84)
        if (not mine.is_valid) or mine.area == 0:
            return None, [], "invalid_geometry"
        my_area = _geod_area(polygon_wgs84)
        if my_area <= 0:
            return None, [], "zero_area"
    except Exception as e:
        return None, [], f"persistence_failed: {e}"
    if not other_scenes:
        # Convention: the count INCLUDES the current scene, so a candidate
        # seen nowhere else still has persistence_count = 1.
        return 1, [], "single_scene_default"
    try:
        matched = []
        for sc in other_scenes or []:
            sid = str(sc.get("scene_id", ""))
            if sid and sid == str(scene_id):
                continue  # never count the same scene twice
            sts = _parse_time(sc.get("acquisition_time"))
            if sts is not None and acquisition_time is not None:
                if abs((sts - acquisition_time).total_seconds()) > float(window_h) * 3600.0:
                    continue
            hit = False
            for pg in sc.get("polygons_wgs84", []) or []:
                try:
                    other = _shape(pg)
                    if (not other.is_valid) or other.area == 0:
                        continue
                    inter = mine.intersection(other)
                    if inter.is_empty:
                        continue
                    ia = _geod_area(inter.__geo_interface__)
                    oa = _geod_area(pg)
                    denom = my_area + oa - ia
                    iou = (ia / denom) if denom > 0 else 0.0
                    if iou >= float(iou_thresh):
                        hit = True
                        break
                except Exception:
                    continue
            if hit:
                matched.append(sid or f"scene_{len(matched)}")
        seen, uniq = set(), []
        for s in matched:
            if s not in seen:
                seen.add(s)
                uniq.append(s)
        return 1 + len(uniq), uniq, "ok"
    except Exception as e:
        return None, [], f"persistence_failed: {e}"


def _read_csv_records(path, required):
    """Read a CSV of contextual records; returns list of dicts (raw strings)."""
    import csv
    recs = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        missing = [c for c in required if c not in cols]
        if missing:
            raise ValueError(f"{path}: missing required columns {missing} (have {sorted(cols)})")
        for row in reader:
            recs.append({k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()})
    return recs


def _load_other_scenes(paths, scene_ids, times):
    """Load external scene polygons for persistence matching (GeoJSON files)."""
    scenes = []
    for i, p in enumerate(paths or []):
        sid = scene_ids[i] if scene_ids and i < len(scene_ids) else Path(p).stem
        tm = times[i] if times and i < len(times) else None
        try:
            gdf = gpd.read_file(p)
            if gdf.crs is not None and str(gdf.crs) != "EPSG:4326":
                gdf = gdf.to_crs(epsg=4326)
            polys = []
            for geom in gdf.geometry:
                try:
                    if geom is not None and (not geom.is_empty):
                        polys.append(geom.__geo_interface__)
                except Exception:
                    continue
            scenes.append({"scene_id": str(sid), "acquisition_time": tm,
                           "polygons_wgs84": polys})
        except Exception as e:
            warnings.warn(f"[persistence] skipping {p}: {e}")
    return scenes


def _validate_b2_entry(entry, texture_config):
    """Per-candidate B2 validation; returns a list of issue strings (empty = clean).

    Checks: non-negative area/perimeter, finite elongation >= 0,
    boundary_irregularity None-or-(finite and >= ~1 for non-degenerate),
    finite GLCM means, config consistency, explicit units (km/h, km).
    Never rejects the scene -- issues are recorded, not fatal.
    """
    issues = []
    try:
        a = entry.get("area_m2")
        if a is not None and (not np.isfinite(float(a)) or float(a) < 0):
            issues.append("invalid_area_m2")
        p = entry.get("perimeter_m")
        if p is not None and (not np.isfinite(float(p)) or float(p) <= 0):
            issues.append("invalid_perimeter_m")
        e = entry.get("elongation")
        if e is not None and (not np.isfinite(float(e)) or float(e) < 0):
            issues.append("invalid_elongation")
        b = entry.get("boundary_irregularity")
        if b is not None:
            bv = float(b)
            if not np.isfinite(bv):
                issues.append("nonfinite_boundary_irregularity")
            elif bv < 0.999 and entry.get("area_m2"):
                issues.append(f"boundary_irregularity_below_1:{bv:.4f}")
        t = (entry.get("texture") or {}).get("glcm") or {}
        for k in ("contrast_mean", "homogeneity_mean", "energy_mean", "correlation_mean"):
            if k in t and not np.isfinite(float(t[k])):
                issues.append(f"nonfinite_glcm_{k}")
        if texture_config:
            if entry.get("glcm_config_snapshot") != {
                    "levels": texture_config.get("levels"),
                    "sar_min_db": texture_config.get("sar_min_db"),
                    "sar_max_db": texture_config.get("sar_max_db"),
                    "distances": texture_config.get("distances"),
                    "angles_degrees": texture_config.get("angles_degrees")}:
                issues.append("glcm_config_mismatch")
        pc = entry.get("persistence_count")
        if pc is not None and (not isinstance(pc, int) or pc < 1):
            issues.append("invalid_persistence_count")
    except Exception as ex:
        issues.append(f"validation_failed: {ex}")
    return issues


def _gis_attributes(polygons_native, candidates):
    """B2 scalar attributes per exported polygon, aligned with polygons_native.

    Each polygon is matched to the candidate whose native polygon contains the
    polygon's representative point (connectivity differences between labeling
    and vectorization can make counts/order differ, so positional matching is
    used instead of assuming order). Unmatched polygons get nulls.
    Returns a list of dicts with full B2 field names (renamed to SHP_FIELD_MAP
    aliases only for the Shapefile write).
    """
    rows = []
    cand_polys = []
    for c in candidates:
        try:
            cp = c.get("polygon_native")
            cand_polys.append(cp if cp is not None else None)
        except Exception:
            cand_polys.append(None)
    for g in polygons_native:
        row = {k: None for k in SHP_FIELD_MAP}
        try:
            geom = shp_shape(g)
            pt = geom.representative_point()
            match = None
            for c, cp in zip(candidates, cand_polys):
                try:
                    if cp is not None and cp.contains(pt):
                        match = c
                        break
                except Exception:
                    continue
            if match is not None:
                t = (match.get("texture") or {}).get("glcm") or {}
                row.update({
                    "candidate_id": match.get("candidate_id"),
                    "area_m2": match.get("area_m2"),
                    "perimeter_m": match.get("perimeter_m"),
                    "elongation": match.get("elongation"),
                    "boundary_irregularity": match.get("boundary_irregularity"),
                    "edge_sharpness": match.get("edge_sharpness"),
                    "mean_backscatter": match.get("mean_backscatter"),
                    "std_backscatter": match.get("std_backscatter"),
                    "glcm_contrast": t.get("contrast_mean"),
                    "glcm_homogeneity": t.get("homogeneity_mean"),
                    "glcm_energy": t.get("energy_mean"),
                    "glcm_correlation": t.get("correlation_mean"),
                    "wind_speed_kmh": match.get("wind_speed_kmh"),
                    "distance_to_nearest_vessel_km": match.get("distance_to_nearest_vessel_km"),
                    "persistence_count": match.get("persistence_count"),
                })
        except Exception:
            pass
        rows.append(row)
    return rows


def load_sar_band(source_image: Path, shape_hw: tuple, band: int = GLCM_DEFAULT_BAND):
    """Read the EXPLICITLY selected SAR backscatter band (float64 HxW).

    Args:
        band: 1-based raster band index (--glcm-band). Must satisfy
            1 <= band <= src.count; anything else raises ValueError.
            Bands are NEVER averaged -- VV/VH combination without a deliberate
            feature-design decision would change the physical meaning.

    Polarization is reported ONLY if the raster metadata states it
    (_detect_polarization); otherwise "unknown" -- never inferred from values.

    Raises ValueError on height/width mismatch -- the source must be the exact
    image the mask was generated from; silent resizing would misalign
    candidates.

    Returns (sar, band_note, sar_info) where sar_info holds band index,
    polarization, dtype, band count and whole-scene value statistics (QC only).
    """
    try:
        with rasterio.open(source_image) as src:
            if (src.height, src.width) != (shape_hw[0], shape_hw[1]):
                raise ValueError(
                    f"Source SAR size {(src.height, src.width)} != mask size "
                    f"{tuple(shape_hw)}; refusing to resize. Pass the exact source "
                    f"image given to infer_pipeline.py via --source-image."
                )
            if not 1 <= int(band) <= src.count:
                raise ValueError(
                    f"--glcm-band={band} is invalid for '{source_image}' "
                    f"which has {src.count} band(s); use 1..{src.count}."
                )
            band = int(band)
            polarization = _detect_polarization(src, band)
            sar = src.read(band).astype(np.float64)
            note = (f"{src.count} band(s), dtype={src.dtypes[band - 1]}; "
                    f"texture band={band} selected explicitly "
                    f"(polarization={polarization}); bands are never averaged")
            if src.count > 1:
                warnings.warn(
                    f"[texture] {source_image}: multi-band raster ({src.count} bands); "
                    f"using band {band} as SAR backscatter (polarization={polarization}) -- "
                    f"confirm this is the intended Sentinel-1 band from your export recipe."
                )
            sar_info = {"sar_band": band, "sar_polarization": polarization,
                        "band_count": int(src.count), "dtype": str(src.dtypes[band - 1])}
            sar_info.update({f"sar_{k}_db": v for k, v in sar_band_stats(sar).items()})
            return sar, note, sar_info
    except rasterio.errors.RasterioIOError as e:
        raise RuntimeError(f"Could not open source SAR image '{source_image}' with rasterio: {e}") from e


def extract_candidate_textures(cleaned: np.ndarray, sar: np.ndarray,
                               valid_sar: np.ndarray | None, stem: str,
                               levels: int = GLCM_DEFAULT_LEVELS,
                               min_db: float = GLCM_DEFAULT_MIN_DB,
                               max_db: float = GLCM_DEFAULT_MAX_DB,
                               distances=None, angles_deg=None,
                               symmetric: bool = GLCM_DEFAULT_SYMMETRIC,
                               min_texture_pixels: int = GLCM_DEFAULT_MIN_TEXTURE_PIXELS,
                               sar_band: int = GLCM_DEFAULT_BAND,
                               sar_polarization: str = "unknown",
                               range_source: str = "manual",
                               context: dict | None = None):
    """Mask-aware GLCM/Haralick extraction, one feature set per candidate.

    Args:
        cleaned: final binary candidate mask (H, W) AFTER morphology + NoData
            filtering -- the same mask exported as polygons.
        sar: ORIGINAL SAR values of the EXPLICITLY selected band (H, W),
            pixel-aligned with `cleaned`. Fixed-range normalization
            (min_db..max_db) applies identically to every candidate so
            features stay comparable -- never per-candidate normalized.
        valid_sar: boolean (H, W) valid-data mask, or None if unknown (then
            every finite SAR pixel counts as valid; NaN/Inf always excluded).
        distances/angles_deg: GLCM geometry; defaults [1,2] / [0,45,90,135].
        sar_band/sar_polarization: recorded verbatim in texture_config for
            reproducibility ("unknown" unless raster metadata proves otherwise).
        context: optional B2 context dict with keys transform, crs, is_geo,
            acquisition_time (datetime|None), weather_records (list),
            weather_window_h, wind_override (dict|None), ais_records (list),
            ais_window_h, other_scenes (list), persist_iou, persist_window_h,
            scene_id. Missing context -> contextual B2 fields stay null.

    Returns:
        (candidates, texture_config): candidates is a list of per-candidate
        dicts (candidate_id, bbox, pixel counts, texture or texture_status);
        texture_config records the generation settings for reproducibility.
    """
    if not _HAS_SKIMAGE_GLCMS:
        raise SystemExit("Missing dependency for texture extraction. Run: pip install scikit-image")
    if distances is None:
        distances = list(GLCM_DEFAULT_DISTANCES)
    if angles_deg is None:
        angles_deg = list(GLCM_DEFAULT_ANGLES_DEG)
    distances = [int(d) for d in distances]
    angles_deg = [float(a) for a in angles_deg]
    if min_db >= max_db:
        raise ValueError(f"--glcm-min-db ({min_db}) must be < --glcm-max-db ({max_db})")
    if levels < 2:
        raise ValueError(f"--glcm-levels must be >= 2, got {levels}")

    texture_config = {
        "method": "GLCM_Haralick",
        "sar_band": int(sar_band),
        "sar_polarization": str(sar_polarization),
        "range_source": str(range_source),
        "levels": int(levels),
        "sar_min_db": float(min_db),
        "sar_max_db": float(max_db),
        "distances": distances,
        "angles_degrees": [int(a) if float(a).is_integer() else float(a) for a in angles_deg],
        "symmetric": bool(symmetric),
        "masked": True,
        "normalization": "fixed_db_range",
        "quantization": f"uniform_{int(levels)}_levels",
        "min_texture_pixels": int(min_texture_pixels),
        "note": ("Mask-aware pair counting: only pairs with BOTH pixels inside "
                 "the candidate AND valid SAR contribute; background/NoData never "
                 "enter the GLCM. Feature definitions: contrast=sum P(i,j)*(i-j)^2; "
                 "homogeneity=sum P(i,j)/(1+|i-j|) [note: this is the |i-j| IDM variant "
                 "mandated by the pipeline spec, whereas skimage graycoprops uses "
                 "1/(1+(i-j)^2) -- both equal 1.0 on single-cell GLCMs]; energy=sqrt(ASM); "
                 "correlation via GLCM marginal means/stds. "
                 "zero-variance correlation convention: 1.0 if all GLCM mass is on "
                 "the diagonal else 0.0. Descriptive only -- NOT an oil detector."),
    }

    print(f"[texture] {stem}: extracting GLCM features "
          f"(band={sar_band} polarization={sar_polarization} levels={levels} db=[{min_db},{max_db}] "
          f"distances={distances} "
          f"angles={[int(a) if float(a).is_integer() else a for a in angles_deg]} symmetric={symmetric})...")

    structure = np.ones((3, 3), dtype=bool)  # 8-connectivity
    labeled, n_candidates = ndi.label(cleaned.astype(bool), structure=structure)
    objects = ndi.find_objects(labeled)
    print(f"[texture] {stem}: {n_candidates} connected candidate(s) in cleaned mask")

    finite_sar = np.isfinite(sar)
    base_valid = valid_sar if valid_sar is not None else np.ones_like(cleaned, dtype=bool)
    offsets = _glcm_offsets(distances, angles_deg)
    ctx = context or {}
    transform = ctx.get("transform")
    crs = ctx.get("crs")
    is_geo = bool(ctx.get("is_geo", False))
    acq_time = ctx.get("acquisition_time")
    scene_id = ctx.get("scene_id", stem)

    candidates = []
    n_ok = 0
    for cid in range(1, n_candidates + 1):
        sl = objects[cid - 1]
        if sl is None:  # empty label slot; skip safely, never crash
            continue
        ys, xs = sl
        ymin, ymax = ys.start, ys.stop - 1
        xmin, xmax = xs.start, xs.stop - 1
        cand_mask = (labeled[ys, xs] == cid)  # 2D, spatial layout preserved
        sar_crop = sar[ys, xs]                # 2D SAR crop, same window
        full_mask = (labeled == cid)  # full-scene mask: boundary bands extend past the bbox
        texture_valid = cand_mask & base_valid[ys, xs] & finite_sar[ys, xs]

        cand_px = int(cand_mask.sum())
        valid_px = int(texture_valid.sum())
        frac = float(valid_px / cand_px) if cand_px > 0 else 0.0
        print(f"[texture] candidate {cid}: {valid_px} valid SAR pixels "
              f"({frac:.2f} of {cand_px} candidate px), bbox x=[{xmin},{xmax}] y=[{ymin},{ymax}]")

        entry = {
            "candidate_id": cid,
            "bbox_xyxy": [int(xmin), int(ymin), int(xmax), int(ymax)],
            "bbox_hw": [int(ymax - ymin + 1), int(xmax - xmin + 1)],
            "candidate_pixel_count": cand_px,
        }
        if cand_px == 0:
            entry.update({"texture": None, "texture_status": "empty_candidate"})
            print(f"[texture] candidate {cid}: empty candidate; texture skipped")
            candidates.append(entry)
            continue
        if valid_px == 0:
            entry.update({"texture": None, "texture_status": "no_valid_sar_pixels",
                          "valid_pixel_count": 0, "valid_pixel_fraction": 0.0, "glcm_pair_count": 0})
            print(f"[texture] candidate {cid}: no valid SAR pixels; texture skipped")
            candidates.append(entry)
            continue
        if valid_px < min_texture_pixels:
            entry.update({"texture": None, "texture_status": "insufficient_valid_pixels",
                          "valid_pixel_count": valid_px, "valid_pixel_fraction": frac, "glcm_pair_count": 0})
            print(f"[texture] candidate {cid}: insufficient valid SAR pixels "
                  f"({valid_px} < {min_texture_pixels}); texture skipped")
            candidates.append(entry)
            continue

        quant_crop = _quantize_sar(sar_crop, min_db, max_db, levels)
        assert quant_crop.min() >= 0 and quant_crop.max() < levels, \
            f"quantization out of range [0,{levels - 1}]"
        # Clip-fraction QC on the VALID candidate pixels only: if (nearly) all
        # pixels sit outside [min_db, max_db], quantization collapses to a
        # single level and a degenerate (contrast=0, energy=1) GLCM is the
        # mathematically correct outcome -- flag it instead of misreading it.
        raw_valid = sar_crop[texture_valid]
        clip_lo = float((raw_valid < min_db).mean())
        clip_hi = float((raw_valid > max_db).mean())
        if clip_lo + clip_hi > 0.95:
            warnings.warn(
                f"[texture] WARNING: candidate {cid} (band={sar_band}, range=[{min_db},{max_db}]): "
                f"{(clip_lo + clip_hi) * 100:.1f}% of valid SAR pixels clip outside the configured "
                f"GLCM range (lower={clip_lo * 100:.1f}%, upper={clip_hi * 100:.1f}%). "
                f"Most texture values will collapse to one quantization level. The GLCM "
                f"implementation is NOT broken -- verify the selected band and "
                f"--glcm-min-db/--glcm-max-db against the Sentinel-1 export recipe."
            )
        glcms, pair_counts = _masked_pair_counts(quant_crop, texture_valid, levels, offsets, symmetric)
        total_pairs = int(sum(pair_counts))
        if total_pairs == 0:
            entry.update({"texture": None, "texture_status": "no_valid_glcm_pairs",
                          "valid_pixel_count": valid_px, "valid_pixel_fraction": frac, "glcm_pair_count": 0})
            print(f"[texture] candidate {cid}: no valid GLCM pairs (isolated pixels); texture skipped")
            candidates.append(entry)
            continue

        per_combo = [_haralick_from_glcm(p) for p in glcms]
        keys = ["contrast", "homogeneity", "energy", "correlation"]
        glcm_summary = {}
        for k in keys:
            vals = np.array([c[k] for c in per_combo], dtype=np.float64)
            glcm_summary[f"{k}_mean"] = float(np.mean(vals))
            glcm_summary[f"{k}_std"] = float(np.std(vals))  # directional/anisotropy info
        glcm_summary["asm_mean"] = float(np.mean([c["asm"] for c in per_combo]))
        glcm_summary = {k: (float(v) if np.isfinite(v) else 0.0) for k, v in glcm_summary.items()}

        entry.update({
            "valid_pixel_count": valid_px,
            "valid_pixel_fraction": frac,
            "clip_fraction_lower": clip_lo,
            "clip_fraction_upper": clip_hi,
            "quantized_min": int(quant_crop[texture_valid].min()),
            "quantized_max": int(quant_crop[texture_valid].max()),
            "quantized_unique_levels": int(len(np.unique(quant_crop[texture_valid]))),
            "quantized_levels": [int(v) for v in np.unique(quant_crop[texture_valid]).tolist()],
            "glcm_pair_count": total_pairs,
            "glcm_pair_count_per_offset": [int(c) for c in pair_counts],
            "texture": {"glcm": glcm_summary},
            "texture_status": "ok",
        })
        # ---- B2 features: SAR-backed (mean/std/edge) ----
        bs_mean, bs_std = _backscatter_stats(sar_crop, texture_valid)
        entry["mean_backscatter"] = bs_mean
        entry["std_backscatter"] = bs_std
        edge_val, edge_reason = _edge_sharpness(sar, full_mask)
        entry["edge_sharpness"] = edge_val
        entry["edge_status"] = edge_reason
        elong = _elongation(cand_mask)
        entry["elongation"] = elong
        # ---- B2 features: geometry (metric perimeter) ----
        if transform is not None:
            metrics = _candidate_polygon_metrics(full_mask, transform, crs, is_geo)
        else:
            metrics = {"polygon_native": None, "polygon_wgs84": None,
                       "perimeter_m": None, "perimeter_status": "no_transform"}
        entry["perimeter_m"] = metrics["perimeter_m"]
        entry["perimeter_status"] = metrics["perimeter_status"]
        # Shapely object kept for GIS attribute mapping; stripped before JSON
        # serialization in _meta_base (polygon_wgs84 dict form is kept).
        entry["polygon_native"] = metrics["polygon_native"]
        entry["polygon_wgs84"] = metrics["polygon_wgs84"]
        centroid = _centroid_lonlat(metrics["polygon_wgs84"],
                                    [int(xmin), int(ymin), int(xmax), int(ymax)],
                                    transform) if transform is not None else None
        entry["centroid_lonlat"] = list(centroid) if centroid else None
        # area_m2 is stamped later in run_postprocess; irregularity needs it,
        # so compute a provisional metric area here when possible.
        entry["_area_m2_provisional"] = None
        try:
            if metrics["polygon_native"] is not None and is_geo and crs is not None:
                if crs.is_projected:
                    entry["_area_m2_provisional"] = float(metrics["polygon_native"].area)
                elif metrics["polygon_wgs84"] is not None:
                    entry["_area_m2_provisional"] = _geod_area(metrics["polygon_wgs84"])
        except Exception:
            pass
        entry["boundary_irregularity"] = _boundary_irregularity(
            metrics["perimeter_m"], entry["_area_m2_provisional"])
        # ---- B2 features: contextual (wind / AIS / persistence) ----
        wind_val, wind_matched, wind_reason = (None, None, "no_weather_context")
        if ctx.get("wind_override") is not None:
            wo = ctx["wind_override"]
            try:
                wind_val = float(wo["wind_speed_kmh"])
                wind_matched = {"wind_speed_kmh": wind_val,
                                "timestamp": wo.get("timestamp"),
                                "source": wo.get("source", "manual_override")}
                wind_reason = "ok_manual_override"
            except Exception:
                wind_reason = "invalid_wind_override"
        elif ctx.get("weather_records"):
            wind_val, wind_matched, wind_reason = _match_wind(
                centroid, acq_time, ctx["weather_records"],
                ctx.get("weather_window_h", 3.0))
        entry["wind_speed_kmh"] = wind_val
        entry["wind_match"] = wind_matched
        entry["wind_status"] = wind_reason
        ves_km, ves_detail, ves_reason = _nearest_vessel_km(
            centroid, acq_time, ctx.get("ais_records") or [],
            ctx.get("ais_window_h", 24.0)) if ctx.get("ais_records") else (None, None, "no_ais_source")
        entry["distance_to_nearest_vessel_km"] = ves_km
        entry["vessel_match"] = ves_detail
        entry["vessel_status"] = ves_reason
        p_count, p_scenes, p_reason = _persistence_count(
            metrics["polygon_wgs84"], scene_id, acq_time,
            ctx.get("other_scenes"), ctx.get("persist_iou", 0.1),
            ctx.get("persist_window_h", 720.0))
        entry["persistence_count"] = p_count
        entry["persistence_scenes"] = p_scenes
        entry["persistence_status"] = p_reason
        entry["glcm_config_snapshot"] = {
            "levels": int(levels), "sar_min_db": float(min_db),
            "sar_max_db": float(max_db), "distances": distances,
            "angles_degrees": [int(a) if float(a).is_integer() else float(a) for a in angles_deg]}
        entry["validation_warnings"] = _validate_b2_entry(entry, texture_config)
        print(f"[texture] candidate {cid}: contrast={glcm_summary['contrast_mean']:.3f} "
              f"homogeneity={glcm_summary['homogeneity_mean']:.3f} "
              f"energy={glcm_summary['energy_mean']:.3f} "
              f"correlation={glcm_summary['correlation_mean']:.3f} "
              f"(pairs={total_pairs})")
        n_ok += 1
        candidates.append(entry)

    print(f"[texture] extracted texture for {n_ok}/{len(candidates)} candidate(s)")
    # Uniform schema: skipped candidates carry explicit nulls, never fake zeros.
    for e in candidates:
        for k in ("mean_backscatter", "std_backscatter", "edge_sharpness",
                  "edge_status", "elongation", "perimeter_m",
                  "perimeter_status", "polygon_native", "polygon_wgs84",
                  "centroid_lonlat",
                  "boundary_irregularity", "wind_speed_kmh", "wind_match",
                  "wind_status", "distance_to_nearest_vessel_km",
                  "vessel_match", "vessel_status", "persistence_count",
                  "persistence_scenes", "persistence_status",
                  "glcm_config_snapshot", "validation_warnings"):
            e.setdefault(k, None)
    return candidates, texture_config


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run_postprocess(mask_path, source_image, output_dir, manual_bounds=None,
                    min_object_px=20, min_hole_px=64, opening_radius=0, closing_radius=1,
                    glcm_levels=GLCM_DEFAULT_LEVELS, glcm_min_db=None,
                    glcm_max_db=None, glcm_distances=None,
                    glcm_angles=None, glcm_symmetric=GLCM_DEFAULT_SYMMETRIC,
                    glcm_min_pixels=GLCM_DEFAULT_MIN_TEXTURE_PIXELS,
                    glcm_band=GLCM_DEFAULT_BAND,
                    acquisition_time=None, source_id=None,
                    wind_speed_kmh=None, wind_timestamp=None, wind_source="manual_override",
                    weather_csv=None, weather_window_h=3.0,
                    ais_csv=None, ais_window_h=24.0,
                    persist_scene=None, persist_time=None, persist_scene_id=None,
                    persist_iou=0.1, persist_window_h=720.0,
                    enable_glcm=True):
    mask_path = Path(mask_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = mask_path.stem.replace("_mask", "")

    # ------------------------------------------------------------------
    # Read and validate the model mask.
    # ------------------------------------------------------------------
    with Image.open(mask_path) as img:
        gray = np.array(img.convert("L"))

    h, w = gray.shape

    # The inference pipeline normally writes a binary 0/255 PNG. Keep the
    # threshold at 127, but print enough information to catch a wrong/empty
    # mask path immediately.
    unique_values = np.unique(gray)
    raw_mask = gray > 127
    px_before = int(raw_mask.sum())

    print(f"[input] mask={mask_path}")
    print(f"[input] shape={w}x{h}, dtype={gray.dtype}, min={int(gray.min())}, "
          f"max={int(gray.max())}, oil_px={px_before}")

    if px_before == 0:
        preview = unique_values[:20].tolist()
        suffix = "..." if unique_values.size > 20 else ""
        warnings.warn(
            f"[mask] {stem}: input mask contains 0 pixels above threshold 127. "
            f"Unique grayscale values: {preview}{suffix}. "
            f"Post-processing cannot recover detections from an empty mask. "
            f"Check that --mask points to the actual *_mask.png produced by "
            f"infer_pipeline.py."
        )

    # ------------------------------------------------------------------
    # Conservative cleanup.
    # ------------------------------------------------------------------
    cleaned = clean_mask(
        raw_mask,
        min_object_px=min_object_px,
        min_hole_px=min_hole_px,
        opening_radius=opening_radius,
        closing_radius=closing_radius,
    )

    px_after = int(cleaned.sum())

    # Safety guard: never let morphology silently turn a non-empty model
    # prediction into an empty spill. If that happens, fall back to the raw
    # binary prediction. This makes the GIS pipeline fail-safe while still
    # reporting that the requested cleanup was too aggressive.
    if px_before > 0 and px_after == 0:
        warnings.warn(
            f"[cleanup] {stem}: requested cleanup erased ALL {px_before} "
            f"predicted-oil pixels. Falling back to the raw model mask so the "
            f"GIS output is not silently blank. Reduce --min-object-px and/or "
            f"--opening-radius if you want stronger cleanup."
        )
        cleaned = raw_mask.copy()
        px_after = int(cleaned.sum())

    print(f"[cleanup] {stem}: {px_before} -> {px_after} oil px "
         f"({'+' if px_after >= px_before else ''}{px_after - px_before} px change)")

    # NoData masking: strip any predicted "oil" that sits over invalid/no-swath
    # pixels in the source raster (see get_valid_data_mask docstring for why
    # this matters -- it's the single most common false-positive source for
    # GEE Sentinel-1 exports).
    valid = get_valid_data_mask(Path(source_image), (h, w))
    if valid is not None:
        px_over_nodata = int((cleaned & ~valid).sum())
        if px_over_nodata > 0:
            print(f"[nodata] {stem}: dropping {px_over_nodata} predicted-oil px that sit over "
                 f"nodata/no-swath area in the source raster")
        cleaned = cleaned & valid
    else:
        warnings.warn(
            f"[nodata] {stem}: source raster has no declared nodata value/alpha band -- "
            f"could NOT check for false positives over swath padding. If '{source_image}' "
            f"has black/blank borders (common for Sentinel-1 GEE exports), inspect "
            f"the overlay/mask near image edges manually."
        )

    Image.fromarray((cleaned.astype(np.uint8)) * 255).save(out_dir / f"{stem}_mask_clean.png")

    # ------------------------------------------------------------------
    # POST-MODEL texture: masked GLCM/Haralick per connected candidate,
    # from the ORIGINAL SAR raster (same cleaned mask that is polygonized).
    # ------------------------------------------------------------------
    candidates = []
    texture_config = None
    sar_band_note = None
    sar_input = None
    acq_time = _parse_time(acquisition_time) if acquisition_time else None
    if acquisition_time and acq_time is None:
        warnings.warn(f"[b2] could not parse --acquisition-time '{acquisition_time}'; "
                      f"time-gated matching (weather/AIS/persistence) will be degraded.")
    # ---- B2 contextual inputs (all optional; absent -> null, never synthetic) ----
    weather_records, weather_status = [], "no_weather_csv"
    if weather_csv:
        try:
            weather_records = _read_csv_records(
                weather_csv, ["lat", "lon", "timestamp", "wind_speed_kmh"])
            weather_status = f"{len(weather_records)} records from {weather_csv}"
        except Exception as e:
            warnings.warn(f"[b2] weather CSV unusable: {e}")
    wind_override = None
    if wind_speed_kmh is not None:
        wind_override = {"wind_speed_kmh": float(wind_speed_kmh),
                         "timestamp": wind_timestamp, "source": wind_source}
    ais_records, ais_status = [], "no_ais_csv"
    if ais_csv:
        try:
            ais_records = _read_csv_records(ais_csv, ["lat", "lon"])
            ais_status = f"{len(ais_records)} records from {ais_csv}"
        except Exception as e:
            warnings.warn(f"[b2] AIS CSV unusable: {e}")
    # Resolve georeferencing BEFORE texture so per-candidate metric geometry
    # (perimeter) uses the same transform later used for polygonization.
    transform, crs, is_geo = get_source_transform_and_crs(Path(source_image), manual_bounds, (h, w))
    other_scenes = _load_other_scenes(persist_scene, persist_scene_id, persist_time)
    b2_context = {
        "transform": transform, "crs": crs, "is_geo": is_geo,
        "acquisition_time": acq_time, "scene_id": stem,
        "weather_records": weather_records, "weather_window_h": float(weather_window_h),
        "wind_override": wind_override,
        "ais_records": ais_records, "ais_window_h": float(ais_window_h),
        "other_scenes": other_scenes, "persist_iou": float(persist_iou),
        "persist_window_h": float(persist_window_h),
    }
    print(f"[b2] weather: {weather_status}; ais: {ais_status}; "
          f"persistence scenes: {len(other_scenes)}; acquisition: {acq_time}")
    if enable_glcm and int(cleaned.sum()) > 0:
        try:
            sar, sar_band_note, sar_input = load_sar_band(Path(source_image), (h, w), band=glcm_band)
            print(f"[texture] {stem}: SAR band: {sar_band_note}; "
                  f"finite min={float(np.nanmin(sar)):.3g} max={float(np.nanmax(sar)):.3g}")
            res_min_db, res_max_db, range_source = resolve_glcm_range(
                sar, glcm_min_db, glcm_max_db)
            print(f"[texture] {stem}: GLCM range [{res_min_db:.3g},{res_max_db:.3g}] "
                  f"(source={range_source})")
            if float(np.nanmin(sar)) >= res_max_db or float(np.nanmax(sar)) <= res_min_db:
                warnings.warn(
                    f"[texture] {stem}: all SAR values fall outside the clip range "
                    f"[{res_min_db},{res_max_db}] dB -- quantization will saturate to a "
                    f"constant level and texture will be degenerate. If the source is a "
                    f"0..255 PNG/preview rather than dB backscatter, texture features are "
                    f"not physically meaningful (GIS outputs are unaffected)."
                )
            candidates, texture_config = extract_candidate_textures(
                cleaned, sar, valid, stem,
                levels=glcm_levels, min_db=res_min_db, max_db=res_max_db,
                distances=glcm_distances, angles_deg=glcm_angles,
                symmetric=glcm_symmetric, min_texture_pixels=glcm_min_pixels,
                sar_band=sar_input["sar_band"],
                sar_polarization=sar_input["sar_polarization"],
                range_source=range_source,
                context=b2_context,
            )
        except ValueError:
            raise  # bad band / range / dimension mismatch: fail loudly, never silently resize
        except Exception as e:
            warnings.warn(f"[texture] {stem}: texture extraction failed ({e}); "
                          f"continuing with GIS outputs but no texture features.")
            candidates, texture_config = [], None
    elif enable_glcm:
        print(f"[texture] {stem}: empty cleaned mask -- no candidates for texture extraction")
        candidates, texture_config = [], {
            "method": "GLCM_Haralick",
            "sar_band": int(glcm_band), "sar_polarization": "unknown",
            "levels": int(glcm_levels),
            "sar_min_db": glcm_min_db, "sar_max_db": glcm_max_db,
            "range_source": "unresolved_empty_mask",
            "distances": [int(d) for d in (glcm_distances or list(GLCM_DEFAULT_DISTANCES))],
            "angles_degrees": list(glcm_angles or list(GLCM_DEFAULT_ANGLES_DEG)),
            "symmetric": bool(glcm_symmetric), "masked": True,
            "normalization": "fixed_db_range",
            "quantization": f"uniform_{int(glcm_levels)}_levels",
        }

    polygons_native = mask_to_polygons(cleaned, transform)

    n_polys = len(polygons_native)
    print(f"[polygons] {stem}: extracted {n_polys} polygon(s)")

    # Per-candidate metric area (GIS outputs themselves are untouched).
    # Projected CRS: pixels * pixel footprint. Geographic CRS: apportion the
    # UTM-based total by pixel share (documented approximation). Pixel space:
    # areas stay in px (area_m2 null -- never fabricated).
    per_candidate_area_m2 = [None] * len(candidates)
    try:
        if candidates and is_geo and crs is not None and crs.is_projected:
            px_area = abs(float(transform.a) * float(transform.e))
            for i, c in enumerate(candidates):
                per_candidate_area_m2[i] = float(c["candidate_pixel_count"] * px_area)
    except Exception as e:
        warnings.warn(f"[area] could not compute per-candidate areas: {e}")
    for i, c in enumerate(candidates):
        c["area_m2"] = per_candidate_area_m2[i]
        c["area_px"] = int(c["candidate_pixel_count"])
        # Finalize B2 shape features against the stamped area (consistent units):
        # prefer the provisional geodesic/planar polygon area when the stamped
        # area is an apportioned estimate; otherwise use the stamped area.
        try:
            prov = c.pop("_area_m2_provisional", None)
            final_area = c["area_m2"] if c["area_m2"] is not None else prov
            if prov is not None and c["area_m2"] is None:
                c["area_m2"] = float(prov)
                final_area = float(prov)
            c["boundary_irregularity"] = _boundary_irregularity(
                c.get("perimeter_m"), final_area)
            c["validation_warnings"] = _validate_b2_entry(c, texture_config)
        except Exception as e:
            warnings.warn(f"[b2] candidate {c.get('candidate_id')}: finalize failed: {e}")
    n_warn = sum(1 for c in candidates if c.get("validation_warnings"))
    print(f"[b2] validation: {n_warn}/{len(candidates)} candidate(s) with warnings")

    def _meta_base(extra):
        for c in candidates:
            # Shapely objects are needed for GIS attribute mapping but are not
            # JSON-serializable; the lon/lat dict form is kept as polygon_wgs84.
            c.pop("polygon_native", None)
        base = {"stem": stem, "n_polygons": n_polys, "n_candidates": len(candidates),
                "sar_band": sar_band_note, "sar_input": sar_input,
                "texture_config": texture_config, "shp_field_map": SHP_FIELD_MAP,
                "scene_datetime": acq_time.isoformat() if acq_time else None,
                "source_id": source_id or stem,
                "b2_context_status": {
                    "weather": weather_status, "ais": ais_status,
                    "persistence_scenes": len(other_scenes)},
                "candidates": candidates}
        base.update(extra)
        return base

    if not is_geo:
        warnings.warn(
            f"\n{'!'*70}\n"
            f"NOT GEOREFERENCED: '{source_image}' has no usable CRS/transform "
            f"and no --manual-bounds was given.\n"
            f"Output polygons are in RAW PIXEL COORDINATES, not real-world "
            f"lat/lon or map coordinates. Files are tagged accordingly.\n"
            f"To get real coordinates: supply a source GeoTIFF with embedded "
            f"CRS, or pass --manual-bounds 'min_lon,min_lat,max_lon,max_lat'.\n"
            f"{'!'*70}"
        )
        tag = "PIXEL_SPACE_NOT_GEOREFERENCED"
        _attr_rows = _gis_attributes(polygons_native, candidates)
        gdf_native = gpd.GeoDataFrame(
            {"id": range(n_polys), "source": [tag] * n_polys, **{
                k: [_r[k] for _r in _attr_rows] for k in SHP_FIELD_MAP}},
            geometry=[shp_shape(g) for g in polygons_native], crs=None,
        )
        gdf_shp = gpd.GeoDataFrame(
            {"id": range(n_polys), **{
                SHP_FIELD_MAP[k]: [_r[k] for _r in _attr_rows] for k in SHP_FIELD_MAP}},
            geometry=[shp_shape(g) for g in polygons_native], crs=None,
        )
        for _col in SHP_FIELD_MAP.values():
            try:
                gdf_shp[_col] = pd.to_numeric(gdf_shp[_col], errors="coerce")
            except Exception:
                pass
        geojson_path = out_dir / f"{stem}_spill.{tag}.geojson"
        shp_path = out_dir / f"{stem}_spill.{tag}.shp"
        gdf_native.to_file(geojson_path, driver="GeoJSON")
        if n_polys > 0:
            gdf_shp.to_file(shp_path, driver="ESRI Shapefile")
        else:
            print("  [note] no polygons to write to shapefile (empty mask)")
        print(f"[write] {geojson_path.name}, {shp_path.name if n_polys else '(shapefile skipped, no polygons)'}")
        meta_path = out_dir / f"{stem}_spill_meta.json"
        result = _meta_base({"georeferenced": False,
                             "geojson": str(geojson_path),
                             "shapefile": str(shp_path) if n_polys else None,
                             "approx_area_m2": None})
        with open(meta_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"[write] {meta_path.name} (pixel-space; per-candidate area_m2=null)")
        return result

    # Georeferenced path: write native-CRS shapefile + WGS84 GeoJSON.
    # Geometry frames are built first (area needs them); B2 attribute columns
    # are assigned AFTER the per-candidate areas are final so GeoJSON/SHP/meta
    # always agree. Geometry/IDs/CRS behavior unchanged.
    gdf_native = gpd.GeoDataFrame(
        {"id": range(n_polys)},
        geometry=[shp_shape(g) for g in polygons_native], crs=crs.to_wkt(),
    )
    gdf_shp = gpd.GeoDataFrame(
        {"id": range(n_polys)},
        geometry=[shp_shape(g) for g in polygons_native], crs=crs.to_wkt(),
    )

    total_area_m2 = None
    try:
        if n_polys > 0:
            if crs.is_projected:
                # Already in a metric CRS (e.g. UTM from GEE) -- use directly,
                # exact, no reprojection distortion.
                total_area_m2 = float(gdf_native.geometry.area.sum())
            else:
                # Geographic CRS (degrees): reproject to the correct local UTM
                # zone for an accurate area, NOT Web Mercator (EPSG:3857), whose
                # area distortion grows with latitude and would be wrong here.
                utm_crs = gdf_native.estimate_utm_crs()
                total_area_m2 = float(gdf_native.to_crs(utm_crs).geometry.area.sum())
    except Exception as e:
        warnings.warn(f"[area] could not compute area estimate: {e}")

    if total_area_m2 is not None:
        area_method = "exact, native projected CRS" if crs.is_projected else "reprojected to local UTM zone"
        print(f"[area] spill area: {total_area_m2:,.0f} m^2 "
              f"({total_area_m2/1e6:.4f} km^2) -- {area_method}")
        if candidates and (crs is not None and not crs.is_projected):
            total_px = sum(c["candidate_pixel_count"] for c in candidates) or 1
            for c in candidates:
                c["area_m2"] = float(total_area_m2 * c["candidate_pixel_count"] / total_px)
            # Area changed after finalization -> refresh dependent B2 fields.
            for c in candidates:
                c["boundary_irregularity"] = _boundary_irregularity(
                    c.get("perimeter_m"), c.get("area_m2"))
                c["validation_warnings"] = _validate_b2_entry(c, texture_config)

    # B2 scalars ride along as attributes (full names in GeoJSON, <=10-char
    # SHP aliases per shp_field_map).
    _attr_rows = _gis_attributes(polygons_native, candidates)
    for _k in SHP_FIELD_MAP:
        gdf_native[_k] = [_r[_k] for _r in _attr_rows]
        try:
            gdf_shp[SHP_FIELD_MAP[_k]] = pd.to_numeric(
                [_r[_k] for _r in _attr_rows], errors="coerce")
        except Exception:
            gdf_shp[SHP_FIELD_MAP[_k]] = [_r[_k] for _r in _attr_rows]
    # DBF has no null-numeric: None -> NaN (written as NULL). GeoJSON/meta
    # keep exact values (ints stay ints).
    shp_path = out_dir / f"{stem}_spill.shp"
    if n_polys > 0:
        gdf_shp.to_file(shp_path, driver="ESRI Shapefile")
    else:
        print("  [note] no polygons to write to shapefile (empty mask)")

    gdf_wgs84 = gdf_native.to_crs(epsg=4326) if n_polys > 0 else gdf_native.set_crs(epsg=4326, allow_override=True)
    geojson_path = out_dir / f"{stem}_spill.geojson"
    gdf_wgs84.to_file(geojson_path, driver="GeoJSON")
    print(f"[write] {geojson_path.name} (EPSG:4326), "
          f"{shp_path.name if n_polys else '(shapefile skipped, no polygons)'} ({crs.to_string()})")

    result = _meta_base({"georeferenced": True, "crs": crs.to_string(),
                         "geojson": str(geojson_path),
                         "shapefile": str(shp_path) if n_polys else None,
                         "approx_area_m2": total_area_m2})
    with open(out_dir / f"{stem}_spill_meta.json", "w") as f:
        json.dump(result, f, indent=2)
    return result


def parse_args():
    p = argparse.ArgumentParser(description="Mask -> cleaned mask -> polygons -> GeoJSON/Shapefile (+ per-candidate GLCM texture)")
    p.add_argument("--mask", required=True, help="Path to {name}_mask.png from infer_pipeline.py")
    p.add_argument("--source-image", required=True,
                  help="Original input image passed to infer_pipeline.py (used to recover geo metadata AND as the SAR source for GLCM texture)")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--manual-bounds", default=None,
                  help='"min_lon,min_lat,max_lon,max_lat" — use when source image has no embedded CRS '
                       "but you know its real-world corner coordinates")
    p.add_argument("--min-object-px", type=int, default=20, help="Drop blobs smaller than this (pixels); default 20")
    p.add_argument("--min-hole-px", type=int, default=64, help="Fill interior holes smaller than this (pixels)")
    p.add_argument("--opening-radius", type=int, default=0, help="Morphological opening disk radius; default 0 to preserve thin oil streaks")
    p.add_argument("--closing-radius", type=int, default=1, help="Morphological closing disk radius; default 1")
    p.add_argument("--glcm-band", type=int, default=GLCM_DEFAULT_BAND,
                   help="1-based raster band used as SAR backscatter for GLCM texture "
                        "(default 1; configurable default, not a validated polarization choice -- "
                        "select the intended Sentinel-1 band from your export recipe; bands are never averaged)")
    p.add_argument("--glcm-levels", type=int, default=GLCM_DEFAULT_LEVELS,
                   help="GLCM gray levels after uniform quantization; default 32")
    p.add_argument("--glcm-min-db", type=float, default=None,
                   help="Lower dB clip (default: auto = scene p1 percentile; "
                        "explicit value recorded as manual)")
    p.add_argument("--glcm-max-db", type=float, default=None,
                   help="Upper dB clip (default: auto = scene p99 percentile)")
    p.add_argument("--glcm-distances", type=int, nargs="+", default=list(GLCM_DEFAULT_DISTANCES),
                   help="GLCM pixel distances, e.g. --glcm-distances 1 2")
    p.add_argument("--glcm-angles", type=float, nargs="+", default=list(GLCM_DEFAULT_ANGLES_DEG),
                   help="GLCM angles in degrees, e.g. --glcm-angles 0 45 90 135")
    p.add_argument("--glcm-min-pixels", type=int, default=GLCM_DEFAULT_MIN_TEXTURE_PIXELS,
                   help="Minimum valid SAR pixels per candidate for texture; below this texture=null")
    p.add_argument("--no-glcm", action="store_true",
                   help="Skip GLCM texture extraction (GIS outputs only)")
    p.add_argument("--glcm-asymmetric", action="store_true",
                   help="Use asymmetric (directional) GLCM counting; default is symmetric")
    p.add_argument("--acquisition-time", default=None,
                   help="Scene acquisition timestamp (ISO 8601) for time-gated B2 matching")
    p.add_argument("--source-id", default=None,
                   help="Source/scene identifier recorded in B2 metadata (default: mask stem)")
    p.add_argument("--wind-speed-kmh", type=float, default=None,
                   help="Explicit 10-m wind speed (km/h) applied to all candidates; "
                        "recorded with timestamp/source, never synthesized")
    p.add_argument("--wind-timestamp", default=None, help="ISO timestamp of the wind observation")
    p.add_argument("--wind-source", default="manual_override",
                   help="Provenance label for the wind value")
    p.add_argument("--weather-csv", default=None,
                   help="CSV with lat,lon,timestamp,wind_speed_kmh[,source]; nearest sample "
                        "within --weather-window-h is matched per candidate centroid")
    p.add_argument("--weather-window-h", type=float, default=3.0,
                   help="Weather temporal match window in hours (default 3.0, mirrors evidence-fusion)")
    p.add_argument("--ais-csv", default=None,
                   help="CSV with lat,lon[,timestamp,mmsi]; min geodesic distance per candidate")
    p.add_argument("--ais-window-h", type=float, default=24.0,
                   help="AIS temporal match window in hours (default 24.0)")
    p.add_argument("--persist-scene", action="append", default=None,
                   help="Other-scene GeoJSON for persistence matching (repeatable)")
    p.add_argument("--persist-time", action="append", default=None,
                   help="ISO acquisition time per --persist-scene, same order (repeatable)")
    p.add_argument("--persist-scene-id", action="append", default=None,
                   help="Scene id per --persist-scene, same order (repeatable)")
    p.add_argument("--persist-iou", type=float, default=0.1,
                   help="IoU threshold for cross-scene candidate matching (default 0.1)")
    p.add_argument("--persist-window-h", type=float, default=720.0,
                   help="Cross-scene temporal window in hours (default 720 = 30 days)")
    return p.parse_args()


def main():
    args = parse_args()
    run_postprocess(
        mask_path=args.mask, source_image=args.source_image, output_dir=args.output_dir,
        manual_bounds=args.manual_bounds, min_object_px=args.min_object_px, min_hole_px=args.min_hole_px,
        opening_radius=args.opening_radius, closing_radius=args.closing_radius,
        glcm_levels=args.glcm_levels, glcm_min_db=args.glcm_min_db, glcm_max_db=args.glcm_max_db,
        glcm_distances=args.glcm_distances, glcm_angles=args.glcm_angles,
        glcm_symmetric=not args.glcm_asymmetric, glcm_min_pixels=args.glcm_min_pixels,
        glcm_band=args.glcm_band,
        acquisition_time=args.acquisition_time, source_id=args.source_id,
        wind_speed_kmh=args.wind_speed_kmh, wind_timestamp=args.wind_timestamp,
        wind_source=args.wind_source, weather_csv=args.weather_csv,
        weather_window_h=args.weather_window_h,
        ais_csv=args.ais_csv, ais_window_h=args.ais_window_h,
        persist_scene=args.persist_scene, persist_time=args.persist_time,
        persist_scene_id=args.persist_scene_id,
        persist_iou=args.persist_iou, persist_window_h=args.persist_window_h,
        enable_glcm=not args.no_glcm,
    )


if __name__ == "__main__":
    main()
