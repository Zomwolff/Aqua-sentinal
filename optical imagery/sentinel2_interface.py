"""Public interface for Sentinel-2 optical imagery and MADOS CNN inference.

The CNN architecture and band-file conventions are imported from
``train_cnn.py``.  The trained checkpoint remains the source of truth for
normalization, class metadata, and the model weights.

Remote acquisition is intentionally not selected here.  ``get_data`` accepts
an existing local MADOS-style folder or a raster containing the ten bands in
training order, while retaining the location arguments for a future provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
import sys
from typing import Any, Optional, Sequence

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.io import DatasetReader


CHECKPOINT_PATH = Path(__file__).with_name("sentinel2_cnn_10band_best.pth")


@dataclass
class Sentinel2Data:
    """Prepared Sentinel-2 bands plus the B2 reference geospatial metadata."""

    bands: dict[str, np.ndarray]
    profile: dict[str, Any]
    transform: Any
    crs: Any
    source: Path

    @property
    def shape(self) -> tuple[int, int]:
        """Return the common ``(height, width)`` of all prepared bands."""
        return next(iter(self.bands.values())).shape


def _training_module():
    """Load the sibling training module without running its training entrypoint."""
    optical_dir = str(Path(__file__).parent)
    if optical_dir not in sys.path:
        sys.path.insert(0, optical_dir)
    return import_module("train_cnn")


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
            raise ValueError("bbox must be (min_lon, min_lat, max_lon, max_lat)")
        min_lon, min_lat, max_lon, max_lat = bbox
        if not (-180 <= min_lon <= max_lon <= 180):
            raise ValueError("bbox longitude bounds are invalid")
        if not (-90 <= min_lat <= max_lat <= 90):
            raise ValueError("bbox latitude bounds are invalid")


def _read_band(path: Path, shape: Optional[tuple[int, int]] = None) -> np.ndarray:
    with rasterio.open(path) as src:
        if shape is None:
            array = src.read(1)
        else:
            array = src.read(1, out_shape=shape, resampling=Resampling.nearest)
    return array.astype(np.float32, copy=False)


def _load_multiband_raster(path: Path, feature_names: Sequence[str]) -> Sentinel2Data:
    with rasterio.open(path) as src:
        if src.count != len(feature_names):
            raise ValueError(
                f"{path} contains {src.count} bands; expected {len(feature_names)} "
                "bands in the training order"
            )
        arrays = src.read().astype(np.float32, copy=False)
        profile = src.profile.copy()
        transform = src.transform
        crs = src.crs

    return Sentinel2Data(
        bands={name: arrays[index] for index, name in enumerate(feature_names)},
        profile=profile,
        transform=transform,
        crs=crs,
        source=path,
    )


def _load_mados_folder(
    folder: Path,
    feature_names: Sequence[str],
    b2_path: Optional[Path] = None,
) -> Sentinel2Data:
    training = _training_module()
    candidates = [b2_path] if b2_path is not None else sorted(
        folder.glob("**/*_L2R_rhorc_492_*.tif")
    )
    if not candidates:
        raise FileNotFoundError(
            f"No MADOS B2 files found below {folder}; expected "
            "*_L2R_rhorc_492_<patch>.tif"
        )
    if len(candidates) > 1:
        raise ValueError(
            f"{folder} contains multiple MADOS patches; pass one patch file "
            "or a folder containing exactly one patch"
        )

    b2_path = candidates[0]
    prefix = b2_path.name.split("_L2R_")[0]
    patch = b2_path.stem.rsplit("_", 1)[1]
    scene_dir = b2_path.parent.parent
    folder_10 = scene_dir / "10"
    folder_20 = scene_dir / "20"

    paths: dict[str, Path] = {"B2": b2_path}
    for band in feature_names:
        if band == "B2":
            continue
        band_folder = folder_10 if band in {"B3", "B4", "B8"} else folder_20
        path = training.find_band_file(
            band_folder,
            prefix,
            training.BAND_WAVELENGTHS[band],
            patch,
        )
        if path is None:
            raise FileNotFoundError(f"Missing {band} file for MADOS patch {patch}")
        paths[band] = Path(path)

    with rasterio.open(b2_path) as reference:
        target_shape = (reference.height, reference.width)
        profile = reference.profile.copy()
        transform = reference.transform
        crs = reference.crs

    bands = {}
    for band in feature_names:
        shape = None if band in {"B2", "B3", "B4", "B8"} else target_shape
        bands[band] = _read_band(paths[band], shape)

    return Sentinel2Data(
        bands=bands,
        profile=profile,
        transform=transform,
        crs=crs,
        source=b2_path,
    )


def get_data(
    image_path: Optional[str | Path] = None,
    *,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    bbox: Optional[Sequence[float]] = None,
) -> Sentinel2Data:
    """Load and prepare a local Sentinel-2 image for downstream components.

    ``image_path`` may be a ten-band raster whose bands already follow the
    training order, a single MADOS B2 patch file, or a directory containing
    exactly one MADOS patch.  For MADOS files, the wavelength alternatives and
    20 m to B2-shape nearest-neighbor resampling come from ``train_cnn.py``.

    Location arguments validate the future acquisition contract but do not
    select an external provider.  A remote fetcher can later be placed behind
    this function without changing its return type or the consumers.
    """
    _validate_location(latitude, longitude, bbox)
    if image_path is None:
        raise NotImplementedError(
            "No Sentinel-2 acquisition provider is configured yet; pass image_path "
            "to load local imagery"
        )
    path = Path(image_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Sentinel-2 image path does not exist: {path}")

    training = _training_module()
    feature_names = tuple(training.FEATURE_NAMES)
    if path.is_file():
        if "_L2R_rhorc_492_" in path.name:
            return _load_mados_folder(path.parent.parent, feature_names, b2_path=path)
        return _load_multiband_raster(path, feature_names)
    return _load_mados_folder(path, feature_names)


def get_environmental_data(data: Sentinel2Data) -> dict[str, Any]:
    """Return only B4 and B8 with the source spatial metadata preserved."""
    for band in ("B4", "B8"):
        if band not in data.bands:
            raise ValueError(f"Prepared imagery does not contain {band}")
    return {
        "B4": data.bands["B4"],
        "B8": data.bands["B8"],
        "profile": data.profile.copy(),
        "transform": data.transform,
        "crs": data.crs,
    }


def _center_window(data: Sentinel2Data, center: Optional[tuple[int, int]]) -> np.ndarray:
    training = _training_module()
    patch_size = int(training.PATCH_SIZE)
    height, width = data.shape
    if height < patch_size or width < patch_size:
        raise ValueError(f"Imagery is {data.shape}; at least {patch_size}x{patch_size} is required")
    row, col = center or (height // 2, width // 2)
    radius = patch_size // 2
    r0, c0 = row - radius, col - radius
    window = np.stack(
        [data.bands[name][r0:r0 + patch_size, c0:c0 + patch_size] for name in training.FEATURE_NAMES],
        axis=0,
    )
    if window.shape != (len(training.FEATURE_NAMES), patch_size, patch_size):
        raise ValueError("Requested center does not have a complete 32x32 window")
    if not np.all(np.isfinite(window)):
        raise ValueError("Requested Sentinel-2 window contains NaN or infinite values")
    return window


def get_fusion_output(
    data: Sentinel2Data,
    *,
    center: Optional[tuple[int, int]] = None,
    checkpoint_path: str | Path = CHECKPOINT_PATH,
    device: Optional[str] = None,
) -> dict[str, Any]:
    """Run the trained CNN and return oil, class, and all class probabilities."""
    import torch

    training = _training_module()
    checkpoint_file = Path(checkpoint_path).expanduser()
    if not checkpoint_file.exists():
        raise FileNotFoundError(f"CNN checkpoint does not exist: {checkpoint_file}")
    checkpoint = torch.load(checkpoint_file, map_location="cpu", weights_only=False)

    expected = tuple(training.FEATURE_NAMES)
    if tuple(checkpoint["feature_names"]) != expected or checkpoint["patch_size"] != training.PATCH_SIZE:
        raise ValueError("Checkpoint preprocessing metadata does not match train_cnn.py")
    if checkpoint["num_bands"] != len(expected) or checkpoint["num_classes"] != len(checkpoint["class_names"]):
        raise ValueError("Checkpoint model metadata is inconsistent")

    model = training.SpectralSpatialCNN(
        num_bands=checkpoint["num_bands"],
        num_classes=checkpoint["num_classes"],
    )
    model.load_state_dict(checkpoint["model_state_dict"])
    model_device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
    model.to(model_device).eval()

    window = _center_window(data, center)
    mean = np.asarray(checkpoint["normalization_mean"], dtype=np.float32)[:, None, None]
    std = np.asarray(checkpoint["normalization_std"], dtype=np.float32)[:, None, None]
    normalized = (window - mean) / std
    tensor = torch.from_numpy(normalized.astype(np.float32, copy=False)).unsqueeze(0).to(model_device)
    with torch.inference_mode():
        probabilities = torch.softmax(model(tensor), dim=1)[0].cpu().numpy()

    predicted_index = int(np.argmax(probabilities))
    predicted_class = predicted_index + 1
    return {
        "oil_probability": float(probabilities[int(checkpoint["oil_class_id"]) - 1]),
        "predicted_class": predicted_class,
        "predicted_class_name": checkpoint["class_names"][predicted_class],
        "class_probabilities": probabilities.tolist(),
    }


if __name__ == "__main__":
    print("Example: data = get_data('path/to/mados_patch_or_10_band.tif', latitude=19.0, longitude=72.0)")
    print("Example: environmental = get_environmental_data(data)")
    print("Example: fusion = get_fusion_output(data)")