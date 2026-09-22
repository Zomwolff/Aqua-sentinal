from pathlib import Path
import json
import re
import tempfile

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

from .config import CANONICAL_BANDS, EO_NORMALIZATION, FUSION_THRESHOLD, SAR_FUSION_WEIGHT

_BAND_PATTERN = re.compile(r"(?:^|[^A-Z0-9])B(8A|0?[2-8]|1[12])(?:[^A-Z0-9]|$)", re.IGNORECASE)


def _session(path: Path):
    import onnxruntime as ort
    return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])


def _sigmoid(values):
    values = np.asarray(values, dtype=np.float32)
    result = np.empty_like(values, dtype=np.float32)
    positive = values >= 0
    result[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    exp_values = np.exp(values[~positive])
    result[~positive] = exp_values / (1.0 + exp_values)
    return result


def _load_sar_image(path: Path):
    """Match UNet-ResNet34 preprocessing; preserve the two training band positions."""
    from .sar_filter import db_to_linear, lee_filter, linear_to_db
    with rasterio.open(path) as source:
        if source.count != 2:
            raise ValueError("SAR UNet requires two Sigma0-dB bands in training file order; single-band and RGB images are unsupported")
        raw = source.read().astype(np.float64)
        valid = np.all(source.read_masks() > 0, axis=0) & np.all(np.isfinite(raw), axis=0)
    if not np.all(np.isfinite(raw)):
        raise ValueError("SAR input contains NaN/Inf; supply finite calibrated Sigma0-dB data")
    config = json.loads((Path(__file__).resolve().parents[1] / "models/sar/normalization.json").read_text())
    filtered = linear_to_db(lee_filter(db_to_linear(raw), window_size=config["lee_window"]))
    normalized = np.stack([np.clip((filtered[c] - lo) / (hi - lo), 0, 1)
                           for c, (lo, hi) in enumerate(config["bands"])]).astype(np.float32)
    return normalized, valid


def _sar_tiles(image, tile=512, overlap=128):
    height, width = image.shape[1:]
    stride = tile - overlap
    rows = [0] if height <= tile else list(range(0, height - tile + 1, stride))
    cols = [0] if width <= tile else list(range(0, width - tile + 1, stride))
    if rows[-1] != max(height - tile, 0): rows.append(max(height - tile, 0))
    if cols[-1] != max(width - tile, 0): cols.append(max(width - tile, 0))
    padded_height, padded_width = max(height, tile), max(width, tile)
    padded = np.pad(image, ((0, 0), (0, padded_height - height), (0, padded_width - width)), mode="reflect")
    return padded, [(y, x) for y in rows for x in cols], (height, width), (padded_height, padded_width)


def predict_sar_probability(path, model_path):
    """Return stitched oil probability and validity, without thresholding tiles."""
    image, valid = _load_sar_image(Path(path).resolve())
    padded, origins, (height, width), shape = _sar_tiles(image)
    session = _session(model_path)
    input_name = session.get_inputs()[0].name
    accumulated = np.zeros(shape, dtype=np.float64)
    counts = np.zeros(shape, dtype=np.float64)
    for y, x in origins:
        tile = np.ascontiguousarray(padded[:, y:y + 512, x:x + 512][None])
        logits = session.run(None, {input_name: tile})[0]
        probability = _sigmoid(logits)[0, 0]
        accumulated[y:y + 512, x:x + 512] += probability
        counts[y:y + 512, x:x + 512] += 1
    result = (accumulated / np.maximum(counts, 1))[:height, :width].astype(np.float32)
    result[~valid] = 0
    return result, valid


def _band_token(name):
    match = _BAND_PATTERN.search(name.upper())
    if not match: return None
    token = match.group(1).upper()
    return "B8A" if token == "8A" else f"B{int(token)}"


def _band_files(directory: Path):
    found = {}
    for path in directory.iterdir():
        if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}:
            band = _band_token(path.name)
            if band:
                if band in found: raise ValueError(f"Duplicate Sentinel-2 band: {band}")
                found[band] = path
    missing = [band for band in CANONICAL_BANDS if band not in found]
    if missing: raise ValueError(f"Missing required Sentinel-2 bands: {missing}")
    return {band: found[band] for band in CANONICAL_BANDS}


def _prepare_eo(path: Path, temporary: Path):
    if path.is_file():
        with rasterio.open(path) as source:
            selected = {}
            for index, description in enumerate(source.descriptions, 1):
                band = _band_token(description or "")
                if band in CANONICAL_BANDS:
                    if band in selected: raise ValueError(f"Duplicate Sentinel-2 band: {band}")
                    selected[band] = index
            missing = [band for band in CANONICAL_BANDS if band not in selected]
            if missing: raise ValueError(f"Missing required Sentinel-2 bands: {missing}")
            # Keep only the model bands (B2..B12), discard e.g. B1/B9/B10/QA,
            # and write the trained canonical order.
            if source.count == 10 and [selected[b] for b in CANONICAL_BANDS] == list(range(1, 11)): return path
            output = temporary / "sentinel2_10band.tif"
            profile = source.profile.copy(); profile.update(count=10, dtype="float32", nodata=None, compress="deflate")
            with rasterio.open(output, "w", **profile) as destination:
                for out_index, band in enumerate(CANONICAL_BANDS, 1):
                    destination.write(source.read(selected[band]).astype(np.float32), out_index)
                    destination.set_band_description(out_index, band)
        return output
    bands = _band_files(path)
    output = temporary / "sentinel2_10band.tif"
    with rasterio.open(bands["B2"]) as reference:
        if reference.crs is None: raise ValueError("Sentinel-2 reference band has no CRS")
        profile = reference.profile.copy(); profile.update(count=10, dtype="float32", nodata=None, compress="deflate")
        with rasterio.open(output, "w", **profile) as destination:
            for index, band in enumerate(CANONICAL_BANDS, 1):
                with rasterio.open(bands[band]) as source:
                    if source.crs is None: raise ValueError(f"Sentinel-2 band {band} has no CRS")
                    values = np.zeros((reference.height, reference.width), dtype=np.float32)
                    if source.crs == reference.crs and source.width == reference.width and source.height == reference.height and source.transform == reference.transform:
                        values[:] = source.read(1).astype(np.float32)
                    else:
                        reproject(source.read(1).astype(np.float32), values, src_transform=source.transform, src_crs=source.crs, dst_transform=reference.transform, dst_crs=reference.crs, resampling=Resampling.bilinear)
                    destination.write(values, index); destination.set_band_description(index, band)
    return output


def _eo_probability(path: Path, model_path: Path):
    with tempfile.TemporaryDirectory(prefix="fusion_eo_stack_") as temp:
        prepared = _prepare_eo(Path(path).resolve(), Path(temp))
        normalization = json.loads(EO_NORMALIZATION.read_text(encoding="utf-8"))
        mean = np.asarray(normalization["mean"], dtype=np.float32)
        std = np.asarray(normalization["std"], dtype=np.float32)
        with rasterio.open(prepared) as source:
            raw = source.read().astype(np.float32)
            valid = np.all(np.isfinite(raw), axis=0)
            normalized = (raw - mean[:, None, None]) / std[:, None, None]
            normalized[:, ~valid] = 0
            height, width = source.height, source.width
        session = _session(model_path); input_name = session.get_inputs()[0].name
        stride = 224; ys = [0] if height <= 256 else list(range(0, height - 256 + 1, stride)); xs = [0] if width <= 256 else list(range(0, width - 256 + 1, stride))
        if ys[-1] != max(height - 256, 0): ys.append(max(height - 256, 0))
        if xs[-1] != max(width - 256, 0): xs.append(max(width - 256, 0))
        accumulated = np.zeros((height, width), dtype=np.float64); weights = np.zeros_like(accumulated)
        for y in ys:
            for x in xs:
                tile = normalized[:, y:y + 256, x:x + 256]; hh, ww = tile.shape[1:]
                if hh != 256 or ww != 256: tile = np.pad(tile, ((0, 0), (0, 256 - hh), (0, 256 - ww)))
                logits = session.run(None, {input_name: tile[None].astype(np.float32)})[0][0]
                probability = _sigmoid(logits)
                yy, xx = np.mgrid[0:hh, 0:ww]; cy, cx = (hh - 1) / 2, (ww - 1) / 2
                blend = np.maximum(np.exp(-0.5 * (((yy - cy) / max(hh / 3, 1)) ** 2 + ((xx - cx) / max(ww / 3, 1)) ** 2)).astype(np.float32), 1e-3)
                accumulated[y:y + hh, x:x + ww] += probability[:hh, :ww] * blend; weights[y:y + hh, x:x + ww] += blend
        result = (accumulated / np.maximum(weights, 1e-8)).astype(np.float32); result[~valid] = 0
        return result, valid


def predict_eo_probability(path, model_path):
    """Return EO probability and the valid-pixel mask on the EO grid."""
    return _eo_probability(Path(path).resolve(), model_path)


def align_probability_pair(sar_path, sar_probability, sar_valid, eo_path, eo_valid):
    """Align a SAR probability array to the EO input grid using geospatial metadata."""
    with tempfile.TemporaryDirectory(prefix="fusion_pair_") as temp:
        temp = Path(temp)
        sar_raster = temp / "sar_probability.tif"
        eo_raster = temp / "eo_reference.tif"
        with rasterio.open(sar_path) as source:
            profile = source.profile.copy(); profile.update(count=1, dtype="float32", nodata=0)
            with rasterio.open(sar_raster, "w", **profile) as destination:
                destination.write(sar_probability, 1); destination.write_mask(sar_valid.astype(np.uint8) * 255)
        eo_reference = _prepare_eo(Path(eo_path).resolve(), temp)
        with rasterio.open(eo_reference) as source:
            profile = source.profile.copy(); profile.update(count=1, dtype="float32", nodata=0)
            with rasterio.open(eo_raster, "w", **profile) as destination:
                destination.write(np.zeros((source.height, source.width), dtype=np.float32), 1)
                destination.write_mask(eo_valid.astype(np.uint8) * 255)
        with rasterio.open(sar_raster) as sar, rasterio.open(eo_raster) as eo:
            aligned = np.zeros((eo.height, eo.width), dtype=np.float32)
            aligned_valid = np.zeros((eo.height, eo.width), dtype=np.uint8)
            source_values = sar.read(1); source_values[sar.read_masks(1) == 0] = -9999
            reproject(source_values, aligned, src_transform=sar.transform, src_crs=sar.crs, src_nodata=-9999, dst_transform=eo.transform, dst_crs=eo.crs, dst_nodata=0, resampling=Resampling.bilinear)
            reproject((sar.read_masks(1) > 0).astype(np.uint8), aligned_valid, src_transform=sar.transform, src_crs=sar.crs, src_nodata=0, dst_transform=eo.transform, dst_crs=eo.crs, dst_nodata=0, resampling=Resampling.nearest)
            valid = (aligned_valid > 0) & (eo.read_masks(1) > 0)
        return np.clip(aligned, 0, 1), valid


def detect_oil_onnx(sentinel1_path, sentinel2_path, sar_onnx: Path, eo_onnx: Path):
    sar_values, sar_valid = predict_sar_probability(sentinel1_path, sar_onnx)
    eo_values, eo_valid = predict_eo_probability(sentinel2_path, eo_onnx)
    aligned, valid = align_probability_pair(sentinel1_path, sar_values, sar_valid, sentinel2_path, eo_valid)
    fused = SAR_FUSION_WEIGHT * aligned + (1 - SAR_FUSION_WEIGHT) * eo_values
    return ((fused >= FUSION_THRESHOLD) & valid).astype(np.uint8), aligned, eo_values
