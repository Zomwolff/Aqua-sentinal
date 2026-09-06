# Spec: `geo_postprocess.py` — mask → GIS-ready spill boundary

## Goal
Given the `{name}_mask.png` output of `infer_pipeline.py` and the ORIGINAL
source image passed to it, produce a cleaned mask + polygon spill boundary
in real-world coordinates, suitable for loading into QGIS/ArcGIS.

## Pipeline stages (in order)

### 1. Load mask
- Read `{name}_mask.png`, binarize at >127 → boolean (H, W) array.
- This mask is already tiled/stitched to full scene size by
  `infer_pipeline.py` — do not re-tile here.

### 2. Morphological cleanup
- Opening (radius ~2px) to strip speckle noise.
- Closing (radius ~2px) to fill small gaps and smooth jagged edges from
  the raw sigmoid threshold.
- `remove_small_objects` (default 64px) to drop residual false-positive
  blobs.
- `remove_small_holes` (default 64px) to avoid donut-shaped polygons from
  noisy interior pixels.
- Order matters: opening → closing → remove_small_objects →
  remove_small_holes. Expose all four as CLI flags with the defaults above.

### 3. NoData masking — CRITICAL, do not skip
**Why this stage exists:** Sentinel-1 SAR swaths are not axis-aligned
rectangles. Google Earth Engine exports a rectangular bounding box, so the
region outside the actual swath is nodata (commonly value `0`, a sentinel
like `-9999`, or an explicit alpha band). This padding is dark/flat and
segmentation models frequently mistake it for a spill or calm water,
producing a false polygon shaped like the swath edge.

**Required behavior:**
- Open the source raster with rasterio. Determine a valid-data mask via, in
  priority order: (a) `src.nodata` scalar (handle NaN specially), (b)
  per-band `src.nodatavals`, (c) an explicit alpha band via
  `src.colorinterp`.
- If none of these are present, do NOT guess (e.g. do not assume 0 = nodata
  — 0 can be a legitimate dB backscatter value). Instead, proceed without
  filtering and emit a clear warning telling the user to visually check
  edges.
- AND the cleaned mask with the valid-data mask before polygon extraction.
  Log how many predicted-oil pixels were dropped this way.
- Validate: source raster (height, width) must match the mask's (h, w)
  before trusting the valid-data mask; if they differ, warn and skip nodata
  filtering rather than silently misaligning it.

### 4. Georeferencing
Priority order for resolving a transform + CRS:
1. `--manual-bounds "min_lon,min_lat,max_lon,max_lat"` if explicitly given
   → build an affine transform via `rasterio.transform.from_bounds` against
   EPSG:4326.
2. Else, open `--source-image` with rasterio: if it has a non-identity
   affine transform AND a CRS, use those directly. This is the expected
   path for GEE exports (GEE embeds a real CRS — often the scene's UTM
   zone, sometimes EPSG:4326 depending on export settings).
3. Else: there is no way to recover real coordinates. Do not fabricate
   them. Write polygons in raw pixel space, tag every output filename
   with `PIXEL_SPACE_NOT_GEOREFERENCED`, and warn loudly.

Validate: if source raster size != mask size, warn that georeferencing will
be approximate (this can happen if the mask was generated from a
resized/cropped copy of the source).

### 5. Polygon extraction
- Vectorize the (nodata-filtered, cleaned) mask via
  `rasterio.features.shapes(mask, mask=mask.astype(bool), transform=transform)`,
  keeping only `value == 1` geometries.
- Do NOT vectorize the unmasked array (would also extract the background
  as a giant polygon).

### 6. Output
- `{name}_mask_clean.png` — cleaned + nodata-filtered binary mask, same
  size as input.
- `{name}_spill.shp` (+ `.dbf/.shx/.prj/.cpg`) — polygons in the **native**
  CRS resolved in step 4 (e.g. UTM meters straight from GEE — don't
  reproject just to write the shapefile).
- `{name}_spill.geojson` — same polygons reprojected to EPSG:4326
  (lat/lon), since GeoJSON's convention is WGS84.
- `{name}_spill_meta.json` — CRS string, polygon count, area, file paths.
- Skip shapefile write (not an error) if zero polygons remain after
  cleanup — log this rather than crashing on an empty GeoDataFrame.

### 7. Area calculation
- If the native CRS is projected (`crs.is_projected`, e.g. UTM from GEE):
  compute area directly in that CRS. It's already metric — do not
  reproject to Web Mercator, which distorts area by a latitude-dependent
  scale factor and is simply the wrong tool here.
- If the native CRS is geographic (degrees, e.g. EPSG:4326): reproject to
  the correct local UTM zone (`gdf.estimate_utm_crs()`) for area, not Web
  Mercator.
- Report both m² and km².

## Required validation tests (do not skip these — this is where the
real bugs live, not in the happy path)

1. **UTM CRS + nodata wedge** (the realistic GEE Sentinel-1 case): build a
   synthetic raster with a UTM CRS in real meter bounds (not degree values
   mislabeled as UTM — verify the numeric bounds actually correspond to the
   claimed EPSG code, e.g. via `pyproj.Transformer`), carve a nodata
   wedge/border, and inject a mask that has false-positive "oil" over that
   wedge plus one real blob. Assert: final polygon count excludes the
   nodata-wedge polygon, and area matches the real blob's exact native-CRS
   area.
2. **Geographic CRS (EPSG:4326), no declared nodata**: confirm it warns
   about being unable to check for nodata, and that the UTM-zone area
   estimate differs sensibly from naive Web Mercator (compute both and
   diff them to catch regressions).
3. **Plain PNG/JPG, no CRS, no manual bounds**: confirm output files are
   explicitly tagged `PIXEL_SPACE_NOT_GEOREFERENCED` and a loud warning
   fires — never let this path emit a file that looks like real
   coordinates.
4. **Plain PNG/JPG + `--manual-bounds`**: confirm it georeferences
   correctly from the supplied corners and CRS is set to EPSG:4326.
5. **Empty mask** (no oil detected at all): confirm it doesn't crash on
   an empty GeoDataFrame and clearly logs "no polygons."
6. **Mask/source size mismatch**: confirm it warns rather than silently
   producing misaligned geometries.

## Known limitations to leave documented, not silently patched over
- No accuracy metrics exist for polygons on new (non-ground-truth) images
  — same rule as the base pipeline's README.
- `--manual-bounds` assumes the tile is a simple axis-aligned rectangle in
  lat/lon; it cannot correct for any rotation/skew in how the tile was cut.
- Multi-band exports: valid-data detection via nodata/alpha works, but if a
  GEE export has per-band nodata that's inconsistent across bands, the
  current logic ANDs all bands together (a pixel must be valid in every
  band) — confirm this is the intended semantics for your specific export
  recipe, or make it configurable (any-band-valid vs all-bands-valid).
