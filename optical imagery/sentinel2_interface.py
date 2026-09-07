"""
Sentinel-2 U-Net inference interface.

This replaces the old CNN-based interface. It does NOT import or depend on
train_cnn.py.

Supported input:
  1. A single MADOS B2 GeoTIFF patch.
  2. A directory containing exactly one MADOS patch.
  3. A 10-band GeoTIFF whose bands are already in this order:
       B2, B3, B4, B5, B6, B7, B8, B8A, B11, B12

The preprocessing matches the U-Net training pipeline:
  - 10 Sentinel-2 bands
  - B5/B6/B7/B8A/B11/B12 resampled to the B2 shape using nearest-neighbor
  - training mean/std loaded from the U-Net checkpoint
  - fixed 256x256 input
  - smaller tiles are zero-padded bottom/right
  - larger tiles are center-cropped
  - U-Net: depth 4, base=32, 15 classes

Example:
    python sentinel2_interface.py "D:/path/to/B2_patch.tif"

    python sentinel2_interface.py "D:/path/to/10_band.tif" ^
        --checkpoint "D:/path/to/sentinel2_unet_seg_best.pth" ^
        --output-dir "inference_output"

The important output for later SAR/EO fusion is:
    oil_probability.npy
    oil_probability.tif

Those contain the continuous Oil Spill probability map, not just a 0/1 mask.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window
from rasterio.windows import transform as window_transform


# ---------------------------------------------------------------------------
# U-Net training configuration
# ---------------------------------------------------------------------------

FEATURE_NAMES = [
    "B2", "B3", "B4", "B5", "B6",
    "B7", "B8", "B8A", "B11", "B12",
]

BAND_WAVELENGTHS = {
    "B2": [492],
    "B3": [559, 560],
    "B4": [665],
    "B5": [704],
    "B6": [739, 740],
    "B7": [780, 783],
    "B8": [833],
    "B8A": [864, 865],
    "B11": [1610, 1614],
    "B12": [2186, 2202],
}

CLASS_NAMES = {
    1: "Marine Debris",
    2: "Dense Sargassum",
    3: "Sparse Floating Algae",
    4: "Natural Organic Material",
    5: "Ship",
    6: "Oil Spill",
    7: "Marine Water",
    8: "Sediment-Laden Water",
    9: "Foam",
    10: "Turbid Water",
    11: "Shallow Water",
    12: "Waves & Wakes",
    13: "Oil Platform",
    14: "Jellyfish",
    15: "Sea snot",
}

NUM_BANDS = 10
NUM_CLASSES = 15
OIL_CLASS_ID = 6
OIL_INDEX = OIL_CLASS_ID - 1
FIXED_SIZE = 256

CHECKPOINT_PATH = Path(__file__).with_name("sentinel2_unet_seg_best.pth")


@dataclass
class Sentinel2Data:
    """Prepared Sentinel-2 bands plus source geospatial metadata."""

    bands: dict[str, np.ndarray]
    profile: dict[str, Any]
    transform: Any
    crs: Any
    source: Path

    @property
    def shape(self) -> tuple[int, int]:
        return next(iter(self.bands.values())).shape


# ---------------------------------------------------------------------------
# U-Net architecture
# Exact architecture used by the training code
# ---------------------------------------------------------------------------

def _build_unet_class():
    import torch
    import torch.nn as nn

    class DoubleConv(nn.Module):
        def __init__(self, in_ch: int, out_ch: int):
            super().__init__()
            self.block = nn.Sequential(
                nn.Conv2d(in_ch, out_ch, kernel_size=3, padding=1),
                nn.BatchNorm2d(out_ch),
                nn.ReLU(inplace=True),
                nn.Conv2d(out_ch, out_ch, kernel_size=3, padding=1),
                nn.BatchNorm2d(out_ch),
                nn.ReLU(inplace=True),
            )

        def forward(self, x):
            return self.block(x)

    class UNet(nn.Module):
        def __init__(
            self,
            in_channels: int = NUM_BANDS,
            num_classes: int = NUM_CLASSES,
            base: int = 32,
        ):
            super().__init__()

            self.enc1 = DoubleConv(in_channels, base)
            self.enc2 = DoubleConv(base, base * 2)
            self.enc3 = DoubleConv(base * 2, base * 4)
            self.enc4 = DoubleConv(base * 4, base * 8)

            self.pool = nn.MaxPool2d(2)

            self.bottleneck = DoubleConv(base * 8, base * 16)

            self.up4 = nn.ConvTranspose2d(
                base * 16, base * 8, kernel_size=2, stride=2
            )
            self.dec4 = DoubleConv(base * 16, base * 8)

            self.up3 = nn.ConvTranspose2d(
                base * 8, base * 4, kernel_size=2, stride=2
            )
            self.dec3 = DoubleConv(base * 8, base * 4)

            self.up2 = nn.ConvTranspose2d(
                base * 4, base * 2, kernel_size=2, stride=2
            )
            self.dec2 = DoubleConv(base * 4, base * 2)

            self.up1 = nn.ConvTranspose2d(
                base * 2, base, kernel_size=2, stride=2
            )
            self.dec1 = DoubleConv(base * 2, base)

            self.classifier = nn.Conv2d(base, num_classes, kernel_size=1)

        def forward(self, x):
            e1 = self.enc1(x)
            e2 = self.enc2(self.pool(e1))
            e3 = self.enc3(self.pool(e2))
            e4 = self.enc4(self.pool(e3))

            b = self.bottleneck(self.pool(e4))

            d4 = self.up4(b)
            d4 = self.dec4(torch.cat([d4, e4], dim=1))

            d3 = self.up3(d4)
            d3 = self.dec3(torch.cat([d3, e3], dim=1))

            d2 = self.up2(d3)
            d2 = self.dec2(torch.cat([d2, e2], dim=1))

            d1 = self.up1(d2)
            d1 = self.dec1(torch.cat([d1, e1], dim=1))

            return self.classifier(d1)

    return UNet


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

def _read_band(
    path: Path,
    shape: Optional[tuple[int, int]] = None,
) -> np.ndarray:
    with rasterio.open(path) as src:
        if shape is None:
            array = src.read(1)
        else:
            array = src.read(
                1,
                out_shape=shape,
                resampling=Resampling.nearest,
            )
    return array.astype(np.float32, copy=False)


def _find_band_file(
    folder: Path,
    prefix: str,
    wavelengths: Sequence[int],
    patch: str,
) -> Optional[Path]:
    # This is the same lookup convention used by the training code.
    for wavelength in wavelengths:
        candidate = folder / (
            f"{prefix}_L2R_rhorc_{wavelength}_{patch}.tif"
        )
        if candidate.exists():
            return candidate
    return None


def _load_mados_folder(
    folder: Path,
    b2_path: Optional[Path] = None,
) -> Sentinel2Data:
    candidates = [b2_path] if b2_path is not None else sorted(
        folder.glob("**/*_L2R_rhorc_492_*.tif")
    )

    if not candidates:
        raise FileNotFoundError(
            f"No MADOS B2 files found below {folder}. "
            "Expected *_L2R_rhorc_492_<patch>.tif"
        )

    if len(candidates) > 1:
        raise ValueError(
            f"{folder} contains multiple MADOS patches. "
            "Pass the specific B2 patch file instead."
        )

    b2_path = candidates[0]
    prefix = b2_path.name.split("_L2R_")[0]
    patch = b2_path.stem.rsplit("_", 1)[1]

    # Same MADOS scene layout used during training.
    scene_dir = b2_path.parent.parent
    folder_10 = scene_dir / "10"
    folder_20 = scene_dir / "20"

    paths: dict[str, Path] = {"B2": b2_path}

    for band in FEATURE_NAMES:
        if band == "B2":
            continue

        band_folder = (
            folder_10 if band in {"B3", "B4", "B8"} else folder_20
        )

        path = _find_band_file(
            band_folder,
            prefix,
            BAND_WAVELENGTHS[band],
            patch,
        )

        if path is None:
            raise FileNotFoundError(
                f"Missing {band} for MADOS patch {patch}.\n"
                f"Looked in: {band_folder}"
            )

        paths[band] = path

    with rasterio.open(b2_path) as reference:
        target_shape = (reference.height, reference.width)
        profile = reference.profile.copy()
        transform = reference.transform
        crs = reference.crs

    bands: dict[str, np.ndarray] = {}

    for band in FEATURE_NAMES:
        # B2/B3/B4/B8 are native 10 m bands.
        # The other bands are resampled to B2's shape, exactly as training.
        shape = None if band in {"B2", "B3", "B4", "B8"} else target_shape
        bands[band] = _read_band(paths[band], shape)

    return Sentinel2Data(
        bands=bands,
        profile=profile,
        transform=transform,
        crs=crs,
        source=b2_path,
    )


def _load_multiband_raster(path: Path) -> Sentinel2Data:
    with rasterio.open(path) as src:
        if src.count != NUM_BANDS:
            raise ValueError(
                f"{path} contains {src.count} bands, but the U-Net expects "
                f"{NUM_BANDS} bands in this exact order:\n"
                f"{FEATURE_NAMES}"
            )

        arrays = src.read().astype(np.float32, copy=False)
        profile = src.profile.copy()
        transform = src.transform
        crs = src.crs

    return Sentinel2Data(
        bands={
            name: arrays[index]
            for index, name in enumerate(FEATURE_NAMES)
        },
        profile=profile,
        transform=transform,
        crs=crs,
        source=path,
    )


def _validate_location(
    latitude: Optional[float],
    longitude: Optional[float],
    bbox: Optional[Sequence[float]],
) -> None:
    if latitude is not None and not -90 <= latitude <= 90:
        raise ValueError("latitude must be between -90 and 90")

    if longitude is not None and not -180 <= longitude <= 180:
        raise ValueError("longitude must be between -180 and 180")

    if bbox is not None:
        if len(bbox) != 4:
            raise ValueError(
                "bbox must be (min_lon, min_lat, max_lon, max_lat)"
            )

        min_lon, min_lat, max_lon, max_lat = bbox

        if not (-180 <= min_lon <= max_lon <= 180):
            raise ValueError("bbox longitude bounds are invalid")

        if not (-90 <= min_lat <= max_lat <= 90):
            raise ValueError("bbox latitude bounds are invalid")


def get_data(
    image_path: str | Path,
    *,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    bbox: Optional[Sequence[float]] = None,
) -> Sentinel2Data:
    """
    Load local Sentinel-2 data.

    A normal RGB/JPEG/PNG image cannot be used because this U-Net expects
    the same 10 Sentinel-2 bands used during training.
    """
    _validate_location(latitude, longitude, bbox)

    path = Path(image_path).expanduser()

    if not path.exists():
        raise FileNotFoundError(f"Sentinel-2 image path does not exist: {path}")

    if path.is_file():
        if "_L2R_rhorc_492_" in path.name:
            return _load_mados_folder(path.parent.parent, b2_path=path)
        return _load_multiband_raster(path)

    return _load_mados_folder(path)


# ---------------------------------------------------------------------------
# Tile preparation
# ---------------------------------------------------------------------------

def _pad_or_crop_to_fixed_size(
    X: np.ndarray,
    size: int = FIXED_SIZE,
) -> tuple[np.ndarray, dict[str, int]]:
    """
    Match the training pipeline:
      smaller -> zero-pad bottom/right
      larger  -> center-crop

    Returns the prepared image and the crop offsets needed for georeferencing.
    """
    h, w, c = X.shape

    if c != NUM_BANDS:
        raise ValueError(f"Expected {NUM_BANDS} bands, got {c}")

    row_offset = 0
    col_offset = 0

    if h < size:
        X = np.pad(
            X,
            ((0, size - h), (0, 0), (0, 0)),
            mode="constant",
        )
    elif h > size:
        row_offset = (h - size) // 2
        X = X[row_offset:row_offset + size, :, :]

    if w < size:
        X = np.pad(
            X,
            ((0, 0), (0, size - w), (0, 0)),
            mode="constant",
        )
    elif w > size:
        col_offset = (w - size) // 2
        X = X[:, col_offset:col_offset + size, :]

    return X, {
        "row_offset": row_offset,
        "col_offset": col_offset,
        "original_height": h,
        "original_width": w,
    }


def _prepare_input(
    data: Sentinel2Data,
) -> tuple[np.ndarray, dict[str, int]]:
    X = np.stack(
        [data.bands[name] for name in FEATURE_NAMES],
        axis=-1,
    ).astype(np.float32, copy=False)

    if X.ndim != 3 or X.shape[-1] != NUM_BANDS:
        raise ValueError(
            f"Prepared input has unexpected shape {X.shape}; "
            f"expected (H, W, {NUM_BANDS})."
        )

    # Same safety behavior as training.
    X = np.nan_to_num(
        X,
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    )

    return _pad_or_crop_to_fixed_size(X, FIXED_SIZE)


def _prepared_transform(
    data: Sentinel2Data,
    info: dict[str, int],
):
    """
    Preserve georeferencing after center-cropping.

    For bottom/right padding, the original top-left transform remains valid.
    """
    row_offset = info["row_offset"]
    col_offset = info["col_offset"]

    if row_offset == 0 and col_offset == 0:
        return data.transform

    return window_transform(
        Window(
            col_offset,
            row_offset,
            FIXED_SIZE,
            FIXED_SIZE,
        ),
        data.transform,
    )


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def _load_model(
    checkpoint_path: str | Path,
    device: Optional[str] = None,
):
    import torch

    checkpoint_file = Path(checkpoint_path).expanduser()

    if not checkpoint_file.exists():
        raise FileNotFoundError(
            f"U-Net checkpoint does not exist: {checkpoint_file}"
        )

    checkpoint = torch.load(
        checkpoint_file,
        map_location="cpu",
        weights_only=False,
    )

    if "model_state_dict" not in checkpoint:
        raise ValueError(
            "Checkpoint does not contain 'model_state_dict'. "
            "This does not look like the U-Net training checkpoint."
        )

    feature_names = tuple(
        checkpoint.get("feature_names", FEATURE_NAMES)
    )

    if feature_names != tuple(FEATURE_NAMES):
        raise ValueError(
            "Checkpoint band order does not match the inference pipeline.\n"
            f"Checkpoint: {feature_names}\n"
            f"Expected:   {tuple(FEATURE_NAMES)}"
        )

    num_bands = int(checkpoint.get("num_bands", NUM_BANDS))
    num_classes = int(checkpoint.get("num_classes", NUM_CLASSES))
    fixed_size = int(checkpoint.get("fixed_size", FIXED_SIZE))

    if num_bands != NUM_BANDS:
        raise ValueError(
            f"Checkpoint expects {num_bands} bands, "
            f"but this pipeline expects {NUM_BANDS}."
        )

    if num_classes != NUM_CLASSES:
        raise ValueError(
            f"Checkpoint has {num_classes} classes, "
            f"but this pipeline expects {NUM_CLASSES}."
        )

    if fixed_size != FIXED_SIZE:
        raise ValueError(
            f"Checkpoint fixed_size={fixed_size}, "
            f"but this pipeline expects {FIXED_SIZE}."
        )

    UNet = _build_unet_class()

    model = UNet(
        in_channels=num_bands,
        num_classes=num_classes,
        base=32,
    )

    model.load_state_dict(checkpoint["model_state_dict"])

    model_device = torch.device(
        device or ("cuda" if torch.cuda.is_available() else "cpu")
    )

    model.to(model_device)
    model.eval()

    return model, checkpoint, model_device


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def predict(
    data: Sentinel2Data,
    *,
    checkpoint_path: str | Path = CHECKPOINT_PATH,
    device: Optional[str] = None,
) -> dict[str, Any]:
    """
    Run U-Net inference on one prepared 256x256 tile.

    Returns:
      oil_probability:
          HxW continuous probability map for Oil Spill.
      class_probabilities:
          15xHxW softmax probabilities.
      predicted_classes:
          HxW MADOS class IDs, 1..15.
      oil_mask_argmax:
          HxW binary mask produced by the same argmax rule used by the
          multiclass segmentation model.
    """
    import torch

    model, checkpoint, model_device = _load_model(
        checkpoint_path,
        device,
    )

    X, prep_info = _prepare_input(data)

    if "normalization_mean" not in checkpoint:
        raise ValueError(
            "Checkpoint is missing normalization_mean."
        )

    if "normalization_std" not in checkpoint:
        raise ValueError(
            "Checkpoint is missing normalization_std."
        )

    mean = np.asarray(
        checkpoint["normalization_mean"],
        dtype=np.float32,
    ).reshape(NUM_BANDS, 1, 1)

    std = np.asarray(
        checkpoint["normalization_std"],
        dtype=np.float32,
    ).reshape(NUM_BANDS, 1, 1)

    X_chw = np.transpose(X, (2, 0, 1)).astype(
        np.float32,
        copy=False,
    )

    # Exact normalization used during U-Net training.
    X_norm = (X_chw - mean) / std

    tensor = torch.from_numpy(X_norm).unsqueeze(0).to(model_device)

    with torch.inference_mode():
        logits = model(tensor)
        probabilities = torch.softmax(
            logits,
            dim=1,
        )[0].cpu().numpy()

    predicted_index = np.argmax(
        probabilities,
        axis=0,
    )

    predicted_classes = (
        predicted_index + 1
    ).astype(np.uint8)

    oil_probability = probabilities[OIL_INDEX].astype(
        np.float32,
        copy=False,
    )

    oil_mask_argmax = (
        predicted_index == OIL_INDEX
    ).astype(np.uint8)

    # The model always runs on 256x256, but smaller source tiles are padded
    # only for inference. Remove that artificial padding before returning or
    # saving predictions, so a 240x240 source produces 240x240 outputs.
    original_height = prep_info["original_height"]
    original_width = prep_info["original_width"]
    if original_height < FIXED_SIZE or original_width < FIXED_SIZE:
        h = min(original_height, FIXED_SIZE)
        w = min(original_width, FIXED_SIZE)
        oil_probability = oil_probability[:h, :w]
        probabilities = probabilities[:, :h, :w]
        predicted_index = predicted_index[:h, :w]
        predicted_classes = predicted_classes[:h, :w]
        oil_mask_argmax = oil_mask_argmax[:h, :w]

    return {
        "oil_probability": oil_probability,
        "class_probabilities": probabilities.astype(np.float32),
        "predicted_classes": predicted_classes,
        "oil_mask_argmax": oil_mask_argmax,
        "source_shape": data.shape,
        "prepared_shape": X.shape[:2],
        "prep_info": prep_info,
        "transform": _prepared_transform(data, prep_info),
        "crs": data.crs,
        "profile": data.profile.copy(),
        "checkpoint": checkpoint,
        "device": str(model_device),
    }


# ---------------------------------------------------------------------------
# Compatibility wrapper
# ---------------------------------------------------------------------------

def get_fusion_output(
    data: Sentinel2Data,
    *,
    center: Optional[tuple[int, int]] = None,
    checkpoint_path: str | Path = CHECKPOINT_PATH,
    device: Optional[str] = None,
) -> dict[str, Any]:
    """
    Backwards-compatible name for callers that used the old interface.

    The old CNN was a 32x32 classifier. The new U-Net is a full-tile
    segmentation model, so `center` is not used.
    """
    if center is not None:
        print(
            "Warning: center= is ignored because the U-Net "
            "performs full-tile 256x256 segmentation."
        )

    result = predict(
        data,
        checkpoint_path=checkpoint_path,
        device=device,
    )

    predicted_classes = result["predicted_classes"]

    center_row = predicted_classes.shape[0] // 2
    center_col = predicted_classes.shape[1] // 2
    center_class = int(
        predicted_classes[center_row, center_col]
    )

    return {
        **result,
        "oil_probability_center": float(
            result["oil_probability"][center_row, center_col]
        ),
        "predicted_class": center_class,
        "predicted_class_name": CLASS_NAMES[center_class],
    }


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------

def _write_geotiff(
    path: Path,
    array: np.ndarray,
    data: Sentinel2Data,
    *,
    transform,
    dtype: str,
    nodata: Optional[float] = None,
) -> None:
    profile = data.profile.copy()

    profile.update(
        driver="GTiff",
        height=array.shape[0],
        width=array.shape[1],
        count=1,
        dtype=dtype,
        transform=transform,
        compress="deflate",
    )

    if nodata is not None:
        profile["nodata"] = nodata

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array.astype(dtype), 1)


def save_outputs(
    result: dict[str, Any],
    data: Sentinel2Data,
    output_dir: str | Path,
    *,
    threshold: float = 0.5,
) -> dict[str, Path]:
    """
    Save prediction products.

    Both the argmax oil mask and an optional probability-threshold mask are
    saved. The continuous oil probability is always saved because that is
    what the future SAR/EO fusion stage should consume.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    transform = result["transform"]

    oil_probability = result["oil_probability"]
    predicted_classes = result["predicted_classes"]
    oil_mask_argmax = result["oil_mask_argmax"]

    oil_mask_threshold = (
        oil_probability >= threshold
    ).astype(np.uint8)

    paths = {
        "oil_probability": output / "oil_probability.tif",
        "predicted_classes": output / "predicted_classes.tif",
        "oil_mask_argmax": output / "oil_mask_argmax.tif",
        "oil_mask_threshold": output / "oil_mask_threshold.tif",
        "oil_probability_npy": output / "oil_probability.npy",
    }

    _write_geotiff(
        paths["oil_probability"],
        oil_probability,
        data,
        transform=transform,
        dtype="float32",
    )

    _write_geotiff(
        paths["predicted_classes"],
        predicted_classes,
        data,
        transform=transform,
        dtype="uint8",
        nodata=0,
    )

    _write_geotiff(
        paths["oil_mask_argmax"],
        oil_mask_argmax,
        data,
        transform=transform,
        dtype="uint8",
        nodata=0,
    )

    _write_geotiff(
        paths["oil_mask_threshold"],
        oil_mask_threshold,
        data,
        transform=transform,
        dtype="uint8",
        nodata=0,
    )

    np.save(
        paths["oil_probability_npy"],
        oil_probability,
    )

    return paths


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Sentinel-2 MADOS U-Net inference."
    )

    parser.add_argument(
        "image",
        help=(
            "Path to a MADOS B2 patch, a directory containing one "
            "MADOS patch, or a 10-band GeoTIFF."
        ),
    )

    parser.add_argument(
        "--checkpoint",
        default=str(CHECKPOINT_PATH),
        help="Path to sentinel2_unet_seg_best.pth",
    )

    parser.add_argument(
        "--output-dir",
        default="inference_output",
        help="Directory for prediction outputs.",
    )

    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help=(
            "Oil probability threshold for the additional threshold-based "
            "binary mask. The model's normal multiclass mask uses argmax."
        ),
    )

    parser.add_argument(
        "--device",
        default=None,
        help='Torch device, e.g. "cpu" or "cuda". Default: auto.',
    )

    args = parser.parse_args()

    if not 0.0 <= args.threshold <= 1.0:
        raise ValueError(
            "--threshold must be between 0 and 1."
        )

    print("=" * 60)
    print("SENTINEL-2 U-NET INFERENCE")
    print("=" * 60)

    print(f"Input:       {args.image}")
    print(f"Checkpoint:  {args.checkpoint}")
    print(f"Output dir:  {args.output_dir}")

    data = get_data(args.image)

    print(f"Source:      {data.source}")
    print(f"Input shape: {data.shape}")
    print(f"Bands:       {', '.join(FEATURE_NAMES)}")

    result = predict(
        data,
        checkpoint_path=args.checkpoint,
        device=args.device,
    )

    oil_probability = result["oil_probability"]
    oil_mask_argmax = result["oil_mask_argmax"]
    oil_mask_threshold = (
        oil_probability >= args.threshold
    )

    print()
    print(f"Device:              {result['device']}")
    print(f"Prepared shape:      {result['prepared_shape']}")
    print(f"Oil threshold:       {args.threshold:.3f}")
    print(
        f"Oil pixels (argmax): {int(oil_mask_argmax.sum())}"
    )
    print(
        f"Oil pixels (threshold): {int(oil_mask_threshold.sum())}"
    )
    print(
        f"Oil fraction (argmax): "
        f"{float(oil_mask_argmax.mean()):.4%}"
    )
    print(
        f"Max oil probability:  {float(oil_probability.max()):.6f}"
    )
    print(
        f"Mean oil probability: {float(oil_probability.mean()):.6f}"
    )

    paths = save_outputs(
        result,
        data,
        args.output_dir,
        threshold=args.threshold,
    )

    print()
    print("Saved:")
    for name, path in paths.items():
        print(f"  {name}: {path}")

    print()
    print("DONE")


if __name__ == "__main__":
    main()
