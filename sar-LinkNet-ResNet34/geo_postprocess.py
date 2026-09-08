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
from skimage.morphology import remove_small_objects, remove_small_holes, opening, closing, disk

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
                               sar_polarization: str = "unknown"):
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
        print(f"[texture] candidate {cid}: contrast={glcm_summary['contrast_mean']:.3f} "
              f"homogeneity={glcm_summary['homogeneity_mean']:.3f} "
              f"energy={glcm_summary['energy_mean']:.3f} "
              f"correlation={glcm_summary['correlation_mean']:.3f} "
              f"(pairs={total_pairs})")
        n_ok += 1
        candidates.append(entry)

    print(f"[texture] extracted texture for {n_ok}/{len(candidates)} candidate(s)")
    return candidates, texture_config


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run_postprocess(mask_path, source_image, output_dir, manual_bounds=None,
                    min_object_px=20, min_hole_px=64, opening_radius=0, closing_radius=1,
                    glcm_levels=GLCM_DEFAULT_LEVELS, glcm_min_db=GLCM_DEFAULT_MIN_DB,
                    glcm_max_db=GLCM_DEFAULT_MAX_DB, glcm_distances=None,
                    glcm_angles=None, glcm_symmetric=GLCM_DEFAULT_SYMMETRIC,
                    glcm_min_pixels=GLCM_DEFAULT_MIN_TEXTURE_PIXELS,
                    glcm_band=GLCM_DEFAULT_BAND,
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
    if enable_glcm and int(cleaned.sum()) > 0:
        try:
            sar, sar_band_note, sar_input = load_sar_band(Path(source_image), (h, w), band=glcm_band)
            print(f"[texture] {stem}: SAR band: {sar_band_note}; "
                  f"finite min={float(np.nanmin(sar)):.3g} max={float(np.nanmax(sar)):.3g}")
            if float(np.nanmin(sar)) >= glcm_max_db or float(np.nanmax(sar)) <= glcm_min_db:
                warnings.warn(
                    f"[texture] {stem}: all SAR values fall outside the clip range "
                    f"[{glcm_min_db},{glcm_max_db}] dB -- quantization will saturate to a "
                    f"constant level and texture will be degenerate. If the source is a "
                    f"0..255 PNG/preview rather than dB backscatter, texture features are "
                    f"not physically meaningful (GIS outputs are unaffected)."
                )
            candidates, texture_config = extract_candidate_textures(
                cleaned, sar, valid, stem,
                levels=glcm_levels, min_db=glcm_min_db, max_db=glcm_max_db,
                distances=glcm_distances, angles_deg=glcm_angles,
                symmetric=glcm_symmetric, min_texture_pixels=glcm_min_pixels,
                sar_band=sar_input["sar_band"],
                sar_polarization=sar_input["sar_polarization"],
            )
        except ValueError:
            raise  # bad band / dimension mismatch: fail loudly, never silently resize
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
            "sar_min_db": float(glcm_min_db), "sar_max_db": float(glcm_max_db),
            "distances": [int(d) for d in (glcm_distances or list(GLCM_DEFAULT_DISTANCES))],
            "angles_degrees": list(glcm_angles or list(GLCM_DEFAULT_ANGLES_DEG)),
            "symmetric": bool(glcm_symmetric), "masked": True,
            "normalization": "fixed_db_range",
            "quantization": f"uniform_{int(glcm_levels)}_levels",
        }

    transform, crs, is_geo = get_source_transform_and_crs(Path(source_image), manual_bounds, (h, w))
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

    def _meta_base(extra):
        base = {"stem": stem, "n_polygons": n_polys, "n_candidates": len(candidates),
                "sar_band": sar_band_note, "sar_input": sar_input,
                "texture_config": texture_config,
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
        gdf_native = gpd.GeoDataFrame(
            {"id": range(n_polys), "source": [tag] * n_polys},
            geometry=[shp_shape(g) for g in polygons_native], crs=None,
        )
        geojson_path = out_dir / f"{stem}_spill.{tag}.geojson"
        shp_path = out_dir / f"{stem}_spill.{tag}.shp"
        gdf_native.to_file(geojson_path, driver="GeoJSON")
        if n_polys > 0:
            gdf_native.to_file(shp_path, driver="ESRI Shapefile")
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
    gdf_native = gpd.GeoDataFrame(
        {"id": range(n_polys)}, geometry=[shp_shape(g) for g in polygons_native], crs=crs.to_wkt(),
    )
    shp_path = out_dir / f"{stem}_spill.shp"
    if n_polys > 0:
        gdf_native.to_file(shp_path, driver="ESRI Shapefile")
    else:
        print("  [note] no polygons to write to shapefile (empty mask)")

    gdf_wgs84 = gdf_native.to_crs(epsg=4326) if n_polys > 0 else gdf_native.set_crs(epsg=4326, allow_override=True)
    geojson_path = out_dir / f"{stem}_spill.geojson"
    gdf_wgs84.to_file(geojson_path, driver="GeoJSON")

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

    print(f"[write] {geojson_path.name} (EPSG:4326), "
         f"{shp_path.name if n_polys else '(shapefile skipped, no polygons)'} ({crs.to_string()})")
    if total_area_m2 is not None:
        area_method = "exact, native projected CRS" if crs.is_projected else "reprojected to local UTM zone"
        print(f"[area] spill area: {total_area_m2:,.0f} m^2 "
             f"({total_area_m2/1e6:.4f} km^2) -- {area_method}")
        if candidates and (crs is not None and not crs.is_projected):
            total_px = sum(c["candidate_pixel_count"] for c in candidates) or 1
            for c in candidates:
                c["area_m2"] = float(total_area_m2 * c["candidate_pixel_count"] / total_px)

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
    p.add_argument("--glcm-min-db", type=float, default=GLCM_DEFAULT_MIN_DB,
                   help="Lower dB clip for SAR normalization (configurable; default -30.0, not universal)")
    p.add_argument("--glcm-max-db", type=float, default=GLCM_DEFAULT_MAX_DB,
                   help="Upper dB clip for SAR normalization (configurable; default 0.0)")
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
        enable_glcm=not args.no_glcm,
    )


if __name__ == "__main__":
    main()
