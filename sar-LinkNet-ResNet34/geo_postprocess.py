"""Post-processing: model mask -> cleaned mask -> polygons -> GIS-ready spill boundary.

Plugs in AFTER infer_pipeline.py. Takes the `{name}_mask.png` it wrote, plus
the ORIGINAL source image (needed to recover geographic reference), and
produces:

    {name}_mask_clean.png     - morphologically cleaned binary mask
    {name}_spill.geojson      - polygon(s) of the spill boundary
    {name}_spill.shp (+ .dbf/.shx/.prj) - same polygons as a shapefile

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

def clean_mask(mask: np.ndarray, min_object_px: int = 64, min_hole_px: int = 64,
               opening_radius: int = 2, closing_radius: int = 2) -> np.ndarray:
    """Speckle removal + hole filling + smoothing on a binary mask.

    Order matters:
      1. Opening (erode-then-dilate) strips isolated speckle noise / thin
         spurious bridges without eating into the main blob much.
      2. Closing (dilate-then-erode) fills small gaps/holes and smooths
         jagged edges from the raw sigmoid threshold.
      3. remove_small_objects drops any remaining blobs below min_object_px
         (these are almost always false positives, not real spill fragments).
      4. remove_small_holes fills tiny interior holes below min_hole_px
         (avoids donut-shaped polygons from noisy interior pixels).
    """
    m = mask.astype(bool)
    if opening_radius > 0:
        m = opening(m, disk(opening_radius))
    if closing_radius > 0:
        m = closing(m, disk(closing_radius))
    m = remove_small_objects(m, min_size=min_object_px)
    m = remove_small_holes(m, area_threshold=min_hole_px)
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
# Main
# --------------------------------------------------------------------------

def run_postprocess(mask_path, source_image, output_dir, manual_bounds=None,
                    min_object_px=64, min_hole_px=64, opening_radius=2, closing_radius=2):
    mask_path = Path(mask_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = mask_path.stem.replace("_mask", "")

    with Image.open(mask_path) as img:
        raw_mask = np.array(img.convert("L")) > 127
    h, w = raw_mask.shape

    cleaned = clean_mask(raw_mask, min_object_px=min_object_px, min_hole_px=min_hole_px,
                         opening_radius=opening_radius, closing_radius=closing_radius)

    px_before, px_after = int(raw_mask.sum()), int(cleaned.sum())
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

    transform, crs, is_geo = get_source_transform_and_crs(Path(source_image), manual_bounds, (h, w))
    polygons_native = mask_to_polygons(cleaned, transform)

    n_polys = len(polygons_native)
    print(f"[polygons] {stem}: extracted {n_polys} polygon(s)")

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
        return {"stem": stem, "georeferenced": False, "n_polygons": n_polys,
               "geojson": str(geojson_path), "shapefile": str(shp_path) if n_polys else None}

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

    result = {"stem": stem, "georeferenced": True, "crs": crs.to_string(), "n_polygons": n_polys,
             "geojson": str(geojson_path), "shapefile": str(shp_path) if n_polys else None,
             "approx_area_m2": total_area_m2}
    with open(out_dir / f"{stem}_spill_meta.json", "w") as f:
        json.dump(result, f, indent=2)
    return result


def parse_args():
    p = argparse.ArgumentParser(description="Mask -> cleaned mask -> polygons -> GeoJSON/Shapefile")
    p.add_argument("--mask", required=True, help="Path to {name}_mask.png from infer_pipeline.py")
    p.add_argument("--source-image", required=True,
                  help="Original input image passed to infer_pipeline.py (used to recover geo metadata)")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--manual-bounds", default=None,
                  help='"min_lon,min_lat,max_lon,max_lat" — use when source image has no embedded CRS '
                       "but you know its real-world corner coordinates")
    p.add_argument("--min-object-px", type=int, default=64, help="Drop blobs smaller than this (pixels)")
    p.add_argument("--min-hole-px", type=int, default=64, help="Fill interior holes smaller than this (pixels)")
    p.add_argument("--opening-radius", type=int, default=2, help="Morphological opening disk radius (speckle removal)")
    p.add_argument("--closing-radius", type=int, default=2, help="Morphological closing disk radius (gap filling)")
    return p.parse_args()


def main():
    args = parse_args()
    run_postprocess(
        mask_path=args.mask, source_image=args.source_image, output_dir=args.output_dir,
        manual_bounds=args.manual_bounds, min_object_px=args.min_object_px, min_hole_px=args.min_hole_px,
        opening_radius=args.opening_radius, closing_radius=args.closing_radius,
    )


if __name__ == "__main__":
    main()
