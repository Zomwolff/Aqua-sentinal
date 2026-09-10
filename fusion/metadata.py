"""Georeferenced fusion outputs and explicit acquisition-time provenance."""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import shapes, geometry_mask, geometry_window
from rasterio.warp import transform_geom, reproject
from rasterio.enums import Resampling
from pyproj import Geod
from PIL import Image
from skimage.feature import graycomatrix, graycoprops
from skimage.measure import label, regionprops
from skimage.morphology import dilation, erosion, disk

from .pipeline import _detect_oil_result, save_mask
from .onnx_runtime import _prepare_eo


def parse_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S")
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def acquisition(paths, explicit=None, ais_time=None):
    if explicit:
        return parse_time(explicit), "explicit"
    for path in paths:
        if not path:
            continue
        files = sorted(p for p in Path(path).iterdir() if p.suffix.lower() in (".tif", ".tiff")) if Path(path).is_dir() else [Path(path)]
        for file in files:
            with rasterio.open(file) as src:
                tags = src.tags()
                for ns in src.tag_namespaces():
                    tags.update(src.tags(ns=ns))
                for key in ("ACQUISITION_TIME", "TIFFTAG_DATETIME"):
                    if tags.get(key):
                        try:
                            return parse_time(tags[key]), "geotiff:" + key
                        except ValueError:
                            continue
    if ais_time:
        return parse_time(ais_time), "ais_fallback"
    raise ValueError("No acquisition time in the imagery or saved AIS. Supply acquisition_time explicitly.")


def preview(values, path):
    values = np.asarray(values, dtype=float)
    finite = values[np.isfinite(values)]
    low, high = np.percentile(finite, [2, 98]) if finite.size else (0, 1)
    values = np.nan_to_num((values - low) / max(high - low, 1e-8))
    Image.fromarray((np.clip(values, 0, 1) * 255).astype("uint8")).save(path)


def candidate_diagnostics(raw, region_mask):
    """Measure shape, texture, and boundary evidence from a model component."""
    raw = np.asarray(raw, dtype=np.float64)
    mask = np.asarray(region_mask, dtype=bool)
    regions = regionprops(label(mask, connectivity=2))
    region = max(regions, key=lambda item: item.area)
    minor = float(region.axis_minor_length)
    shape = {
        "area_px": int(region.area),
        "eccentricity": float(region.eccentricity),
        "elongation": float(region.axis_major_length / minor) if minor > 1e-9 else 20.0,
        "solidity": float(region.solidity),
        "perimeter_area_ratio": float(region.perimeter / region.area),
    }
    finite_values = raw[mask & np.isfinite(raw)]
    mean = float(finite_values.mean())
    std = float(finite_values.std())
    quantized = np.full(mask.shape, 32, dtype=np.uint8)
    if finite_values.max() > finite_values.min():
        scaled = (raw - finite_values.min()) / (finite_values.max() - finite_values.min()) * 31
        quantized[mask] = np.clip(scaled[mask], 0, 31).astype(np.uint8)
    else:
        quantized[mask] = 0
    matrix = graycomatrix(quantized, [1], [0, np.pi / 4, np.pi / 2, 3 * np.pi / 4],
                          levels=33, symmetric=True, normed=False)[:32, :32]
    total = matrix.sum(axis=(0, 1), keepdims=True)
    matrix = matrix / np.maximum(total, 1)
    texture = {
        "contrast": float(graycoprops(matrix, "contrast").mean()),
        "homogeneity": float(graycoprops(matrix, "homogeneity").mean()),
        "energy": float(graycoprops(matrix, "energy").mean()),
        "correlation": float(np.nan_to_num(graycoprops(matrix, "correlation"), nan=1.0).mean()),
        "mean_backscatter": mean,
        "std_backscatter": std,
    }
    outer = dilation(mask, footprint=disk(3)) & ~mask
    inside_edge = mask & ~erosion(mask, footprint=disk(3))
    inside = raw[inside_edge & np.isfinite(raw)]
    outside = raw[outer & np.isfinite(raw)]
    inside_mean = float(inside.mean()) if inside.size else None
    outside_mean = float(outside.mean()) if outside.size else None
    gradient = outside_mean - inside_mean if inside_mean is not None and outside_mean is not None else None
    edge = {"inside_backscatter_db": inside_mean, "outside_backscatter_db": outside_mean,
            "gradient": gradient,
            "classification": None if gradient is None else ("diffuse" if gradient <= 3.0 else "sharp")}
    return shape, texture, edge


def run_fusion(sentinel1_path=None, sentinel2_path=None, *, output_dir,
               acquisition_time=None, scene_id=None, ais_time=None):
    """Run once, preserving source coordinates; IDs here identify candidates only."""
    when, time_source = acquisition([sentinel1_path, sentinel2_path], acquisition_time, ais_time)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = _detect_oil_result(sentinel1_path, sentinel2_path)
    probability = result.pop("probability")
    mask = result.pop("mask")
    prep = out / "prep"
    prep.mkdir(exist_ok=True)
    reference = _prepare_eo(Path(sentinel2_path), prep) if sentinel2_path else sentinel1_path
    # Only Sentinel-1 supplies the georeference of exported spill geometry.
    if sentinel1_path and sentinel2_path:
        with rasterio.open(reference) as eo, rasterio.open(sentinel1_path) as sar:
            aligned_mask = np.zeros((sar.height, sar.width), dtype="uint8")
            aligned_probability = np.zeros(aligned_mask.shape, dtype="float32")
            for source, destination, method in ((mask, aligned_mask, Resampling.nearest),
                                                 (probability, aligned_probability, Resampling.bilinear)):
                reproject(source, destination, src_transform=eo.transform, src_crs=eo.crs,
                          dst_transform=sar.transform, dst_crs=sar.crs, resampling=method)
            mask, probability = aligned_mask, aligned_probability
    if sentinel1_path:
        save_mask(mask, sentinel1_path, out / "final_mask.tif")
    Image.fromarray(mask * 255).save(out / "final_mask.png")
    artifacts = {"final": "final_mask.png", "metadata": "fusion_metadata.json"}
    if sentinel1_path:
        artifacts["mask"] = "final_mask.tif"
    for key, path in (("s1", sentinel1_path), ("s2", reference if sentinel2_path else None)):
        if path:
            with rasterio.open(path) as src:
                preview(src.read(1), out / (key + ".png"))
            artifacts[key] = key + ".png"
    candidates = []
    geod = Geod(ellps="WGS84")
    with rasterio.open(sentinel1_path or reference) as src:
        raw_reference = src.read(1).astype(np.float64)
        if sentinel1_path and src.crs is None:
            raise ValueError("A georeferenced TIFF with a CRS is required")
        for polygon, value in shapes(mask, mask=(mask.astype(bool) if sentinel1_path else np.zeros(mask.shape, bool)), transform=src.transform):
            if not value:
                continue
            window = geometry_window(src, [polygon])
            rows, cols = window.toslices()
            local_mask = mask[rows, cols]
            selected = geometry_mask([polygon], local_mask.shape, src.window_transform(window), invert=True) & local_mask.astype(bool)
            geometry = transform_geom(src.crs, "EPSG:4326", polygon)
            ring_areas = [abs(geod.polygon_area_perimeter(*zip(*ring))[0]) for ring in geometry["coordinates"]]
            area = max(0.0, ring_areas[0] - sum(ring_areas[1:]))
            shape_data, texture_data, edge_data = candidate_diagnostics(raw_reference[rows, cols], selected)
            candidates.append({"candidate_id": str(uuid.uuid4()), "geometry": geometry,
                               "area_m2": area, "area_km2": area / 1e6,
                               "pixel_count": int(selected.sum()),
                               "confidence": float(probability[rows, cols][selected].mean()),
                               "shape": shape_data, "texture": texture_data, "edge": edge_data,
                               "model_version": "onnx-fusion-v1"})
    source = Path(sentinel1_path or sentinel2_path)
    source_name = source.stem if source.is_file() else sorted(p.stem for p in source.iterdir() if p.suffix.lower() in (".tif", ".tiff"))[0]
    result.update(scene_id=scene_id or source_name,
                  acquisition_time=when.isoformat(), acquisition_time_source=time_source,
                  processed_at=datetime.now(timezone.utc).isoformat(),
                  area_m2=sum(c["area_m2"] for c in candidates), candidates=candidates,
                  artifacts=artifacts, model_version="onnx-fusion-v1")
    result["georeference_source"] = "sentinel1" if sentinel1_path else None
    result["output_pixel_count"] = int(mask.sum())
    result["area_m2"] = result["area_m2"] if sentinel1_path else None
    result["area_km2"] = result["area_m2"] / 1e6 if sentinel1_path else None
    (out / "fusion_metadata.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
