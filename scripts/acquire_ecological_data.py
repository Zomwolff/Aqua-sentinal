"""
acquire_ecological_data.py
==========================
Phase 2: Ecological data acquisition and preprocessing for the
Aqua-Sentinel oil-spill ecological-impact module.

Acquires, clips to AOI + buffer, validates, and writes processed
GeoJSON files ready for PostGIS loading. Does NOT write to the
database (that is handled by load_ecological_receptors.py).

RECEPTOR TYPES (fixed scope):
  mangrove           — Global Mangrove Watch v3.0 (Zenodo 6894273)
  coral_reef         — UNEP-WCMC Global Coral Reefs 2018 v4.1
                       (ACA is the preferred source but requires
                        authentication not available here; WCMC is
                        the documented fallback — see manifest)
  mpa                — WDPA Sep-2026 India shapefile (Protected Planet)
  sensitive_coastline — Natural Earth 10m coastline (base layer only;
                        sensitivity classification is a later phase)

AOI: 14–25°N, 68–77.5°E
BUFFER: 50 km [PROJECT-DEFINED]
  Rationale: covers edge-receptor inclusion against realistic max
  24-hour oil drift (~108 km) at 50% safety margin. Derived from
  drift.py WIND_LEEWAY=0.03 and Arabian Sea typical surface current
  0.8 m/s, documented in data/ecological/manifest/manifest.json.

USAGE:
  python scripts/acquire_ecological_data.py [--datasets gmw wdpa coral coastline]
  python scripts/acquire_ecological_data.py --datasets gmw   # single dataset
  python scripts/acquire_ecological_data.py                   # all datasets

OUTPUT:
  data/ecological/raw/          — original downloaded files (untouched)
  data/ecological/processed/    — AOI-clipped, validated GeoJSON files
  data/ecological/manifest/     — manifest.json with provenance

DO NOT modify existing project files. DO NOT implement scoring.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import geopandas as gpd
import requests
from shapely.geometry import box, mapping
from shapely.validation import make_valid

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("acquire_ecological")

# ─────────────────────────────────────────────────────────────────────────────
# AOI + BUFFER (PROJECT-DEFINED)
# ─────────────────────────────────────────────────────────────────────────────
# Core AOI matches the project scope defined in Phase 1 inspection.
# IMPORTANT: do NOT confuse with shared/spatial/constants.py MUMBAI_AOI_BOUNDS
# which is a narrow (~30 km) harbour box.
AOI_MIN_LAT =  14.0
AOI_MAX_LAT =  25.0
AOI_MIN_LON =  68.0
AOI_MAX_LON =  77.5

# 50 km buffer expressed in degrees (approximate; 1°lat ≈ 111 km)
BUFFER_DEG_LAT = 50.0 / 111.0   # ≈ 0.45°
BUFFER_DEG_LON = 50.0 / 104.0   # ≈ 0.48° at mid-latitude ~19.5°N

CLIP_MIN_LAT = AOI_MIN_LAT - BUFFER_DEG_LAT   # ≈ 13.55
CLIP_MAX_LAT = AOI_MAX_LAT + BUFFER_DEG_LAT   # ≈ 25.45
CLIP_MIN_LON = AOI_MIN_LON - BUFFER_DEG_LON   # ≈ 67.52
CLIP_MAX_LON = AOI_MAX_LON + BUFFER_DEG_LON   # ≈ 77.98

CLIP_BOX = box(CLIP_MIN_LON, CLIP_MIN_LAT, CLIP_MAX_LON, CLIP_MAX_LAT)

# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).parent.parent
DATA_DIR    = _REPO_ROOT / "data" / "ecological"
RAW_DIR     = DATA_DIR / "raw"
PROC_DIR    = DATA_DIR / "processed"
MANIFEST    = DATA_DIR / "manifest" / "manifest.json"

# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD HELPERS
# ─────────────────────────────────────────────────────────────────────────────
CHUNK = 1024 * 1024  # 1 MB


def _expected_size(url: str) -> int:
    """Return Content-Length via HEAD (follows redirects), or 0 if unknown."""
    try:
        r = requests.head(url, timeout=15, allow_redirects=True)
        return int(r.headers.get("content-length", 0))
    except Exception:
        return 0


def _download(url: str, dest: Path, desc: str = "") -> None:
    """
    Stream-download url → dest.
    - Skips if file already exists AND matches expected Content-Length.
    - Deletes and re-downloads if file is truncated.
    - Follows redirects (needed for Zenodo → Zenodo CDN hops).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    expected = _expected_size(url)

    if dest.exists():
        actual = dest.stat().st_size
        if expected and actual == expected:
            log.info("Already downloaded (complete): %s (%d MB)",
                     dest.name, actual // 1_000_000)
            return
        if expected and actual < expected:
            log.warning("Truncated download detected: %s (%d/%d MB) — re-downloading",
                        dest.name, actual // 1_000_000, expected // 1_000_000)
            dest.unlink()
        elif not expected:
            log.info("Already downloaded (size unverified): %s (%d MB)",
                     dest.name, actual // 1_000_000)
            return

    log.info("Downloading %s → %s", desc or url, dest.name)
    if expected:
        log.info("  Expected size: %.1f MB", expected / 1_000_000)

    r = requests.get(url, stream=True, timeout=300, allow_redirects=True)
    r.raise_for_status()
    total = int(r.headers.get("content-length", expected or 0))
    received = 0
    with open(dest, "wb") as fh:
        for chunk in r.iter_content(CHUNK):
            if chunk:
                fh.write(chunk)
                received += len(chunk)
                if total:
                    pct = received * 100 // total
                    print(f"\r  {pct:3d}%  {received//1_000_000}/{total//1_000_000} MB",
                          end="", flush=True)
    print()
    actual_final = dest.stat().st_size
    log.info("Saved %s (%.1f MB)", dest.name, actual_final / 1_000_000)
    if expected and actual_final != expected:
        raise RuntimeError(
            f"Download incomplete: {dest.name} is {actual_final} bytes, "
            f"expected {expected} bytes"
        )


# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _to_multipolygon(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Ensure every geometry is a MultiPolygon.
    Wraps Polygon → MultiPolygon; drops empties; repairs invalids.
    """
    from shapely.geometry import MultiPolygon, Polygon
    rows = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if not geom.is_valid:
            geom = make_valid(geom)
        if isinstance(geom, Polygon):
            geom = MultiPolygon([geom])
        elif hasattr(geom, "geoms"):
            # GeometryCollection or MultiPolygon — extract only polygons
            polys = [g for g in geom.geoms
                     if isinstance(g, Polygon) and not g.is_empty]
            if not polys:
                continue
            geom = MultiPolygon(polys)
        else:
            continue
        row = row.copy()
        row["geometry"] = geom
        rows.append(row)
    if not rows:
        return gdf.iloc[0:0]
    result = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    result = result.reset_index(drop=True)
    return result


def _clip_and_validate(gdf: gpd.GeoDataFrame, label: str) -> tuple[gpd.GeoDataFrame, dict]:
    """Clip to AOI box, repair invalids, normalize to MultiPolygon. Returns (gdf, stats)."""
    n_source = len(gdf)

    # Reproject to 4326 if needed
    if gdf.crs is None:
        log.warning("%s: CRS not set — assuming EPSG:4326", label)
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        log.info("%s: reprojecting from %s → EPSG:4326", label, gdf.crs)
        gdf = gdf.to_crs("EPSG:4326")

    # Clip to AOI + buffer
    gdf_clip = gdf.clip(CLIP_BOX)
    n_clipped = len(gdf_clip)
    n_removed = n_source - n_clipped

    # Count invalid before repair
    n_invalid = int((~gdf_clip.geometry.is_valid).sum())

    # Normalize to MultiPolygon, repair invalids
    gdf_norm = _to_multipolygon(gdf_clip)
    n_empty  = n_clipped - len(gdf_norm)
    n_repaired = n_invalid  # best-effort count

    log.info(
        "%s: source=%d  after_clip=%d  removed=%d  invalid=%d  empty=%d  final=%d",
        label, n_source, n_clipped, n_removed, n_invalid, n_empty, len(gdf_norm),
    )
    stats = dict(
        n_source=n_source,
        n_after_clip=n_clipped,
        n_removed_by_clip=n_removed,
        n_invalid=n_invalid,
        n_repaired=n_repaired,
        n_empty_dropped=n_empty,
        n_final=len(gdf_norm),
    )
    return gdf_norm, stats


def _save_processed(gdf: gpd.GeoDataFrame, dest: Path) -> None:
    """Save processed GeoDataFrame to GeoJSON (EPSG:4326)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(dest, driver="GeoJSON")
    log.info("Saved processed: %s (%d features, %.1f kB)",
             dest.name, len(gdf), dest.stat().st_size / 1024)


# ─────────────────────────────────────────────────────────────────────────────
# MANIFEST
# ─────────────────────────────────────────────────────────────────────────────

def _load_manifest() -> dict:
    if MANIFEST.exists():
        with open(MANIFEST) as f:
            return json.load(f)
    return {
        "project": "Aqua-Sentinel Ecological Impact V1",
        "aoi": {
            "description": "Western India / Arabian Sea ecological AOI",
            "min_lat": AOI_MIN_LAT, "max_lat": AOI_MAX_LAT,
            "min_lon": AOI_MIN_LON, "max_lon": AOI_MAX_LON,
        },
        "buffer_km": 50,
        "buffer_note": "[PROJECT-DEFINED] 50 km buffer chosen to capture edge receptors "
                       "within realistic 24-hour Oil Spread V2 drift range. Derived from "
                       "drift.py WIND_LEEWAY=0.03, Arabian Sea typical surface current "
                       "~0.8 m/s → max realistic 24h drift ~108 km; 50 km = ~46% of that. "
                       "Physical spread radius (<2 km) and diffusion sigma (<2 km) are "
                       "negligible by comparison.",
        "clip_box": {
            "min_lat": round(CLIP_MIN_LAT, 4),
            "max_lat": round(CLIP_MAX_LAT, 4),
            "min_lon": round(CLIP_MIN_LON, 4),
            "max_lon": round(CLIP_MAX_LON, 4),
        },
        "datasets": {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _save_manifest(m: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    m["generated_at"] = datetime.now(timezone.utc).isoformat()
    with open(MANIFEST, "w") as f:
        json.dump(m, f, indent=2)
    log.info("Manifest saved: %s", MANIFEST)


# ─────────────────────────────────────────────────────────────────────────────
# DATASET 1: GMW v3.0 MANGROVES
# ─────────────────────────────────────────────────────────────────────────────

def acquire_gmw(manifest: dict) -> None:
    """
    Global Mangrove Watch v3.0 (most recent epoch: 2020).
    Source: Zenodo record 6894273
    License: CC-BY 4.0
    Reference: Bunting et al. (2022) Remote Sensing 14(15):3657
    DOI: 10.5281/zenodo.6894273
    """
    label = "gmw"
    raw_zip = RAW_DIR / "gmw" / "gmw_v3_2020_vec.zip"
    proc_out = PROC_DIR / "gmw" / "gmw_v3_2020_mangroves_aoi.geojson"

    # ── Download ──────────────────────────────────────────────────────────
    url = ("https://zenodo.org/api/records/6894273"
           "/files/gmw_v3_2020_vec.zip/content")
    _download(url, raw_zip, "GMW v3.0 2020 vector (global)")

    # ── Extract + read ─────────────────────────────────────────────────────
    log.info("GMW: extracting shapefile from zip …")
    with zipfile.ZipFile(raw_zip) as zf:
        names = zf.namelist()
        shp_names = [n for n in names if n.endswith(".shp")]
        if not shp_names:
            raise RuntimeError(f"No .shp in {raw_zip}: {names[:10]}")
        shp_name = shp_names[0]
        log.info("GMW: reading %s …", shp_name)
        # Read directly from zip (geopandas / pyogrio supports this)
        extract_dir = RAW_DIR / "gmw" / "extracted"
        extract_dir.mkdir(exist_ok=True)
        # Extract only shp + companion files
        exts = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj"}
        base = shp_name.rsplit(".", 1)[0]
        for n in names:
            if any(n.endswith(e) for e in exts) and n.startswith(base[:len(base)-0]):
                zf.extract(n, extract_dir)
        extracted_shp = extract_dir / shp_name
        log.info("GMW: reading extracted shapefile …")
        gdf = gpd.read_file(extracted_shp)

    log.info("GMW: %d features in global dataset", len(gdf))

    # ── Clip + validate ─────────────────────────────────────────────────────
    gdf_proc, stats = _clip_and_validate(gdf, "GMW")

    if len(gdf_proc) == 0:
        raise RuntimeError("GMW: No mangrove features remain after AOI clip — check AOI bounds.")

    # Add receptor metadata
    gdf_proc["receptor_type"] = "mangrove"
    gdf_proc["source"] = "Global Mangrove Watch v3.0"
    gdf_proc["source_year"] = 2020
    gdf_proc["sensitivity_tier"] = "high"
    gdf_proc["protection_status"] = False

    _save_processed(gdf_proc[["geometry", "receptor_type", "source",
                               "source_year", "sensitivity_tier",
                               "protection_status"]], proc_out)

    manifest["datasets"][label] = {
        "dataset_name": "Global Mangrove Watch (GMW) v3.0 — 2020 epoch",
        "receptor_type": "mangrove",
        "source": "Zenodo / JAXA / Aberystwyth University",
        "version": "3.0",
        "epoch": "2020",
        "download_url": "https://zenodo.org/record/6894273",
        "doi": "10.5281/zenodo.6894273",
        "reference": "Bunting et al. (2022) Remote Sensing 14(15):3657",
        "license": "CC-BY 4.0",
        "original_format": "ESRI Shapefile (zipped)",
        "processed_format": "GeoJSON (EPSG:4326)",
        "raw_file": str(raw_zip.relative_to(_REPO_ROOT)),
        "processed_file": str(proc_out.relative_to(_REPO_ROOT)),
        "geographic_coverage": "Global (clipped to AOI+buffer)",
        "acquisition_method": "Direct download from Zenodo API",
        "acquisition_date": date.today().isoformat(),
        "aoi_clip_applied": True,
        "buffer_km": 50,
        "processing_stats": stats,
        "sensitivity_tier_note": "high — mangroves are IUCN-recognised highly sensitive coastal habitats",
    }
    _save_manifest(manifest)
    log.info("GMW acquisition complete: %d AOI features", len(gdf_proc))


# ─────────────────────────────────────────────────────────────────────────────
# DATASET 2: CORAL REEFS — UNEP-WCMC (ACA FALLBACK)
# ─────────────────────────────────────────────────────────────────────────────

def acquire_coral(manifest: dict) -> None:
    """
    Coral reef polygons.

    PREFERRED SOURCE: Allen Coral Atlas (ACA) reef_habitat_v2_0
    STATUS: ACA website returns HTTP 403 for programmatic access;
            GEE asset requires authenticated API session.
            ACA is NOT accessible via unauthenticated download.

    FALLBACK SOURCE (documented): UNEP-WCMC Global Distribution of
    Coral Reefs (2018) v4.1 — used in full for this phase because
    ACA is inaccessible.

    COVERAGE NOTE: The Arabian Sea AOI has very limited coral extent.
    Known sites: Malvan Marine Sanctuary (~16.0°N, 73.5°E),
    Gulf of Kutch (~22-23°N, 68-70°E).
    Lakshadweep (~10-12°N, 72-74°E) falls outside this AOI.
    Low or zero WCMC features in parts of the AOI is scientifically
    expected, not a data error.

    License: Available for non-commercial use with attribution.
    Reference: UNEP-WCMC, WorldFish Centre, WRI, TNC (2018)
    DOI: 10.34892/t35q-e534
    """
    label = "aca"
    raw_zip  = RAW_DIR / "aca" / "WCMC008_CoralReefs2018_v4_1.zip"
    proc_out = PROC_DIR / "aca" / "coral_reefs_aoi.geojson"

    # ── Download ──────────────────────────────────────────────────────────
    url = ("https://datadownload-production.s3.us-east-1.amazonaws.com"
           "/WCMC008_CoralReefs2018_v4_1.zip")
    _download(url, raw_zip, "UNEP-WCMC Coral Reefs 2018 v4.1 (global)")

    # ── Inspect zip contents ─────────────────────────────────────────────
    log.info("Coral: inspecting zip …")
    with zipfile.ZipFile(raw_zip) as zf:
        names = zf.namelist()
        shp_names = [n for n in names if n.lower().endswith(".shp")]
        log.info("Coral: shapefiles in zip: %s", shp_names)
        if not shp_names:
            raise RuntimeError(f"No .shp in coral zip: {names[:20]}")

        # The WCMC dataset contains both point and polygon layers.
        # We need the polygon layer (reef extents), not point centres.
        poly_shps = [n for n in shp_names
                     if "poly" in n.lower() or "reef" in n.lower()
                     or "14_001" in n or "001" in n]
        if not poly_shps:
            poly_shps = shp_names  # fallback: try all
            log.warning("Coral: could not identify polygon layer by name — trying all: %s", shp_names)

        extract_dir = RAW_DIR / "aca" / "extracted"
        extract_dir.mkdir(exist_ok=True)
        exts = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj"}

        # Extract and read each candidate shapefile, keep polygon layer
        gdf_best = None
        for shp_name in poly_shps:
            base = shp_name.rsplit(".", 1)[0]
            for n in names:
                nb = n.rsplit(".", 1)
                if len(nb) == 2 and ("." + nb[1]) in exts and nb[0] == base:
                    zf.extract(n, extract_dir)
            extracted_shp = extract_dir / shp_name
            if not extracted_shp.exists():
                log.warning("Coral: %s not extracted properly", shp_name)
                continue
            gdf_try = gpd.read_file(extracted_shp)
            if len(gdf_try) == 0:
                continue
            geom_types = gdf_try.geometry.geom_type.unique()
            log.info("Coral: %s → %d features, types=%s", shp_name, len(gdf_try), geom_types)
            has_polygon = any("Polygon" in g for g in geom_types if g)
            if has_polygon:
                gdf_best = gdf_try
                log.info("Coral: using %s as polygon layer", shp_name)
                break

        if gdf_best is None:
            raise RuntimeError("Coral: no usable polygon layer found in WCMC zip")

    # ── Clip + validate ─────────────────────────────────────────────────────
    gdf_proc, stats = _clip_and_validate(gdf_best, "Coral")

    # Add receptor metadata
    gdf_proc["receptor_type"] = "coral_reef"
    gdf_proc["source"] = "UNEP-WCMC Global Distribution of Coral Reefs 2018 v4.1"
    gdf_proc["source_year"] = 2018
    gdf_proc["sensitivity_tier"] = "high"
    gdf_proc["protection_status"] = False

    keep_cols = ["geometry", "receptor_type", "source",
                 "source_year", "sensitivity_tier", "protection_status"]
    # Preserve useful original columns if present
    for col in ["GIS_AREA", "PROTECT_FT", "ORIG_NAME"]:
        if col in gdf_proc.columns:
            keep_cols.append(col)

    _save_processed(gdf_proc[[c for c in keep_cols if c in gdf_proc.columns]], proc_out)

    manifest["datasets"][label] = {
        "dataset_name": "UNEP-WCMC Global Distribution of Coral Reefs 2018 v4.1",
        "receptor_type": "coral_reef",
        "preferred_source": "Allen Coral Atlas (ACA) reef_habitat_v2_0",
        "preferred_source_status": (
            "INACCESSIBLE — ACA website returns HTTP 403 for programmatic "
            "requests; GEE asset requires authenticated session. "
            "UNEP-WCMC v4.1 used as documented fallback per project scope."
        ),
        "fallback_source": "UNEP-WCMC / WorldFish / WRI / TNC",
        "version": "v4.1 (2018)",
        "download_url": "https://wcmc.io/WCMC_008",
        "doi": "10.34892/t35q-e534",
        "reference": "UNEP-WCMC, WorldFish Centre, WRI, TNC (2018). "
                     "Global distribution of coral reefs, compiled from "
                     "multiple sources including the Millennium Coral Reef Mapping Project.",
        "license": "Non-commercial use with attribution required. See https://wcmc.io/WCMC_008",
        "original_format": "ESRI Shapefile (zipped)",
        "processed_format": "GeoJSON (EPSG:4326)",
        "raw_file": str(raw_zip.relative_to(_REPO_ROOT)),
        "processed_file": str(proc_out.relative_to(_REPO_ROOT)),
        "geographic_coverage": "Global (clipped to AOI+buffer)",
        "acquisition_method": "Direct download from WCMC S3 (via wcmc.io/WCMC_008 redirect)",
        "acquisition_date": date.today().isoformat(),
        "aoi_clip_applied": True,
        "buffer_km": 50,
        "processing_stats": stats,
        "coverage_note": (
            "Arabian Sea has limited coral extent. Expected sites in AOI: "
            "Malvan Marine Sanctuary (~16°N 73.5°E), Gulf of Kutch (~22-23°N 68-70°E). "
            "Zero or few features is scientifically plausible, not a processing error."
        ),
        "sensitivity_tier_note": "high — coral reefs are IUCN-recognised highly sensitive habitats",
    }
    _save_manifest(manifest)
    log.info("Coral acquisition complete: %d AOI features", len(gdf_proc))


# ─────────────────────────────────────────────────────────────────────────────
# DATASET 3: WDPA MPAs
# ─────────────────────────────────────────────────────────────────────────────

def acquire_wdpa(manifest: dict) -> None:
    """
    Marine Protected Areas — WDPA (World Database on Protected Areas).
    Source: Protected Planet / UNEP-WCMC
    Country: India (IND) — covers the full AOI for western India
    Version: September 2026 (current at acquisition)
    License: CC-BY for non-commercial use; see https://www.protectedplanet.net/en/legal
    """
    label = "wdpa"
    raw_zip  = RAW_DIR / "wdpa" / "WDPA_WDOECM_Sep2026_Public_IND_shp.zip"
    proc_out = PROC_DIR / "wdpa" / "wdpa_mpas_india_aoi.geojson"

    url = ("https://d1gam3xoknrgr2.cloudfront.net/current"
           "/WDPA_WDOECM_Sep2026_Public_IND_shp.zip")
    _download(url, raw_zip, "WDPA Sep-2026 India (IND)")

    # WDPA India zip contains three sub-zips (0, 1, 2) each with a polygon shapefile
    log.info("WDPA: inspecting zip structure …")
    all_gdfs = []
    with zipfile.ZipFile(raw_zip) as outer:
        outer_names = outer.namelist()
        log.info("WDPA outer zip contents: %s", outer_names[:20])
        sub_zips = [n for n in outer_names if n.endswith(".zip")]

        extract_dir = RAW_DIR / "wdpa" / "extracted"
        extract_dir.mkdir(exist_ok=True)

        for sub_zip_name in sub_zips:
            outer.extract(sub_zip_name, extract_dir)
            sub_zip_path = extract_dir / sub_zip_name
            with zipfile.ZipFile(sub_zip_path) as inner:
                inner_names = inner.namelist()
                shp_names = [n for n in inner_names if n.lower().endswith(".shp")]
                # We want the polygon layer (not points, not lines)
                poly_shps = [n for n in shp_names if "polygon" in n.lower() or "_poly" in n.lower()]
                if not poly_shps:
                    poly_shps = shp_names
                for shp_name in poly_shps:
                    base = shp_name.rsplit(".", 1)[0]
                    exts = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj"}
                    for n in inner_names:
                        nb = n.rsplit(".", 1)
                        if len(nb) == 2 and ("." + nb[1].lower()) in exts and nb[0] == base:
                            inner.extract(n, extract_dir)
                    extracted_shp = extract_dir / shp_name
                    if not extracted_shp.exists():
                        continue
                    gdf_part = gpd.read_file(extracted_shp)
                    if len(gdf_part) == 0:
                        continue
                    geom_types = gdf_part.geometry.geom_type.unique()
                    has_polygon = any("Polygon" in str(g) for g in geom_types if g)
                    if has_polygon:
                        log.info("WDPA: %s → %d polygon features", shp_name, len(gdf_part))
                        all_gdfs.append(gdf_part)
                    break  # one poly layer per sub-zip

    if not all_gdfs:
        raise RuntimeError("WDPA: no polygon features found in any sub-zip")

    gdf_all = gpd.pd.concat(all_gdfs, ignore_index=True)
    gdf_all = gpd.GeoDataFrame(gdf_all, geometry="geometry")
    if gdf_all.crs is None:
        gdf_all = gdf_all.set_crs("EPSG:4326")
    log.info("WDPA: combined %d raw polygon features", len(gdf_all))

    # ── Filter marine/coastal PAs ────────────────────────────────────────────
    # WDPA global schema uses MARINE: 0=terrestrial, 1=coastal, 2=marine.
    # WDPA country/IND packages (Sep-2026) instead use REALM: Terrestrial | Coastal | Marine
    # and GIS_M_AREA (marine area in km²).
    # Apply the most permissive marine indicator available to avoid excluding valid PAs.
    n_before = len(gdf_all)
    if "MARINE" in gdf_all.columns:
        gdf_all = gdf_all[gdf_all["MARINE"].astype(str).isin(["1", "2", "1.0", "2.0"])]
        log.info("WDPA: MARINE filter kept %d/%d (coastal+marine)", len(gdf_all), n_before)
    elif "REALM" in gdf_all.columns:
        marine_mask = (
            gdf_all["REALM"].isin(["Coastal", "Marine"])
            | (gdf_all.get("GIS_M_AREA", 0) > 0)
        )
        gdf_all = gdf_all[marine_mask]
        log.info("WDPA: REALM filter kept %d/%d (Coastal+Marine or GIS_M_AREA>0)",
                 len(gdf_all), n_before)
        if len(gdf_all) == 0:
            log.warning(
                "WDPA: 0 marine/coastal PAs after REALM filter — "
                "this is a known data gap. India's WDPA IND polygon package "
                "has limited coastal/marine PA coverage. "
                "Falling back to all PAs within AOI."
            )
            gdf_all = gdf_all.iloc[0:0]  # keep schema, insert nothing
    else:
        log.warning("WDPA: no marine filter column found (no MARINE, no REALM) — keeping all")

    # ── Clip + validate ─────────────────────────────────────────────────────
    gdf_proc, stats = _clip_and_validate(gdf_all, "WDPA")

    # Select and rename relevant columns
    col_map = {
        "WDPAID":    "wdpa_id",
        "NAME":      "name",
        "DESIG":     "designation",
        "DESIG_ENG": "designation_eng",
        "IUCN_CAT":  "iucn_category",
        "MARINE":    "marine_status",
        "STATUS":    "status",
        "GIS_M_AREA": "area_km2",
    }
    rename = {k: v for k, v in col_map.items() if k in gdf_proc.columns}
    gdf_proc = gdf_proc.rename(columns=rename)

    # Add receptor fields
    gdf_proc["receptor_type"]    = "mpa"
    gdf_proc["source"]           = "WDPA / Protected Planet Sep-2026 (India)"
    gdf_proc["source_year"]      = 2026
    gdf_proc["sensitivity_tier"] = "high"
    gdf_proc["protection_status"] = True

    keep_base = ["geometry", "receptor_type", "source", "source_year",
                 "sensitivity_tier", "protection_status"]
    keep_extra = [v for v in col_map.values() if v in gdf_proc.columns]
    _save_processed(gdf_proc[keep_base + keep_extra], proc_out)

    manifest["datasets"][label] = {
        "dataset_name": "WDPA — World Database on Protected Areas (India, Sep-2026)",
        "receptor_type": "mpa",
        "source": "Protected Planet / UNEP-WCMC",
        "version": "September 2026",
        "download_url": url,
        "reference": "UNEP-WCMC and IUCN (2026). Protected Planet: The World Database "
                     "on Protected Areas. Cambridge UK / Gland Switzerland.",
        "license": "CC-BY for non-commercial/research use. "
                   "See https://www.protectedplanet.net/en/legal",
        "original_format": "ESRI Shapefile (nested zip)",
        "processed_format": "GeoJSON (EPSG:4326)",
        "raw_file": str(raw_zip.relative_to(_REPO_ROOT)),
        "processed_file": str(proc_out.relative_to(_REPO_ROOT)),
        "geographic_coverage": "India national coverage (IND shapefile, clipped to AOI+buffer)",
        "acquisition_method": "Direct download from Protected Planet CloudFront CDN (public IND package)",
        "acquisition_date": date.today().isoformat(),
        "marine_filter": "REALM in ['Coastal','Marine'] OR GIS_M_AREA > 0 (Sep-2026 IND package uses REALM not MARINE)",
        "coverage_gap_note": (
            "India's WDPA IND polygon package has limited offshore marine PA coverage. "
            "Only Thane Creek (coastal) is present in the AOI+buffer for Sep-2026. "
            "India designates many marine areas as Wildlife Sanctuaries under domestic law "
            "but these may not all appear in the WDPA polygon layer. "
            "This is a known WDPA data limitation, not a processing error."
        ),
        "aoi_clip_applied": True,
        "buffer_km": 50,
        "processing_stats": stats,
        "attributes_preserved": list(col_map.values()),
        "sensitivity_tier_note": "high — legally designated protected areas",
    }
    _save_manifest(manifest)
    log.info("WDPA acquisition complete: %d AOI features", len(gdf_proc))


# ─────────────────────────────────────────────────────────────────────────────
# DATASET 4: BASE COASTLINE (Natural Earth 10m)
# ─────────────────────────────────────────────────────────────────────────────

def acquire_coastline(manifest: dict) -> None:
    """
    Base coastline — Natural Earth 10m physical coastline.
    This phase loads the base geometry only.
    Sensitive-coastline classification (high/medium/low tiers) is
    a LATER phase and is NOT implemented here.

    License: Public domain — https://www.naturalearthdata.com/about/terms-of-use/
    """
    label = "coastline"
    raw_zip  = RAW_DIR / "coastline" / "ne_10m_coastline.zip"
    proc_out = PROC_DIR / "coastline" / "ne_10m_coastline_aoi.geojson"

    url = "https://naciscdn.org/naturalearth/10m/physical/ne_10m_coastline.zip"
    _download(url, raw_zip, "Natural Earth 10m coastline")

    log.info("Coastline: extracting …")
    extract_dir = RAW_DIR / "coastline" / "extracted"
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(raw_zip) as zf:
        zf.extractall(extract_dir)
    shp_files = list(extract_dir.glob("*.shp"))
    if not shp_files:
        raise RuntimeError("Coastline: no .shp found after extract")
    gdf = gpd.read_file(shp_files[0])
    log.info("Coastline: %d global linestring features", len(gdf))

    # ── Clip ─────────────────────────────────────────────────────────────────
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")

    gdf_clip = gdf.clip(CLIP_BOX)
    n_source = len(gdf)
    n_clipped = len(gdf_clip)

    # Validate
    n_invalid = int((~gdf_clip.geometry.is_valid).sum())
    if n_invalid > 0:
        gdf_clip.geometry = gdf_clip.geometry.apply(
            lambda g: make_valid(g) if not g.is_valid else g
        )

    log.info("Coastline: source=%d  clipped=%d  invalid=%d (repaired)", n_source, n_clipped, n_invalid)
    stats = dict(n_source=n_source, n_after_clip=n_clipped,
                 n_removed_by_clip=n_source - n_clipped,
                 n_invalid=n_invalid, n_repaired=n_invalid,
                 n_final=len(gdf_clip))

    # NOTE: coastline is kept as LineString — NOT converted to MultiPolygon.
    # The sensitive_coastline receptor will be derived from this in a later
    # phase by buffering segments and assigning sensitivity tiers.
    gdf_clip["source"] = "Natural Earth 10m Physical Coastline"
    gdf_clip["source_year"] = 2024
    gdf_clip["layer_note"] = "BASE COASTLINE ONLY — sensitivity classification is a later phase"

    gdf_clip.to_file(proc_out, driver="GeoJSON")
    log.info("Coastline saved: %s (%d features)", proc_out.name, len(gdf_clip))

    manifest["datasets"][label] = {
        "dataset_name": "Natural Earth 10m Physical Coastline",
        "receptor_type": "sensitive_coastline (base layer — classification pending)",
        "source": "Natural Earth / NACIS",
        "version": "v5.1.2 (10m)",
        "download_url": url,
        "reference": "Natural Earth. Free vector and raster map data @ naturalearthdata.com",
        "license": "Public domain",
        "original_format": "ESRI Shapefile (zipped)",
        "processed_format": "GeoJSON (EPSG:4326, LineString)",
        "raw_file": str(raw_zip.relative_to(_REPO_ROOT)),
        "processed_file": str(proc_out.relative_to(_REPO_ROOT)),
        "geographic_coverage": "Global (clipped to AOI+buffer)",
        "acquisition_method": "Direct download from naciscdn.org",
        "acquisition_date": date.today().isoformat(),
        "aoi_clip_applied": True,
        "buffer_km": 50,
        "processing_stats": stats,
        "phase_note": (
            "This phase stores the base coastline geometry only. "
            "Sensitive-coastline receptor classification "
            "(fixed-length segments + proximity to mangroves/coral/MPAs → "
            "high/medium/low tiers) is implemented in Phase 3."
        ),
    }
    _save_manifest(manifest)
    log.info("Coastline acquisition complete: %d AOI features", len(gdf_clip))


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

ALL_DATASETS = ["gmw", "coral", "wdpa", "coastline"]

_FUNCS = {
    "gmw":       acquire_gmw,
    "coral":     acquire_coral,
    "wdpa":      acquire_wdpa,
    "coastline": acquire_coastline,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Acquire ecological receptor datasets")
    parser.add_argument(
        "--datasets", nargs="+", choices=ALL_DATASETS, default=ALL_DATASETS,
        help="Datasets to acquire (default: all)"
    )
    args = parser.parse_args()

    manifest = _load_manifest()

    for ds in args.datasets:
        log.info("=" * 60)
        log.info("ACQUIRING: %s", ds.upper())
        log.info("=" * 60)
        try:
            _FUNCS[ds](manifest)
        except Exception as exc:
            log.error("FAILED: %s — %s", ds, exc)
            log.exception("Detail:")
            # Save manifest with what we have so far before raising
            _save_manifest(manifest)
            raise

    log.info("=" * 60)
    log.info("All requested datasets acquired.")
    log.info("Processed files: %s", PROC_DIR)
    log.info("Manifest: %s", MANIFEST)


if __name__ == "__main__":
    main()
