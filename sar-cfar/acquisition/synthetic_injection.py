"""Opt-in synthetic SAR slick injection for demo/pipeline validation (Step 7).

Synthetic provenance is always explicit and never confused with real SAR
evidence. Injection is DISABLED by default and must be explicitly enabled.

The injected signal is a smooth, Gaussian-blended elliptical dark region
(``darkness_db`` below the local backscatter), not a hard-edged binary shape.
The original raster/array is never mutated.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import numpy as np
from scipy.ndimage import gaussian_filter


def resolve_synthetic_enabled(
    explicit: Optional[bool],
    env_value: Optional[str],
) -> bool:
    """Decide whether synthetic injection is enabled.

    Precedence: an explicit caller-supplied flag > environment configuration.
    ``explicit`` may be omitted (None); a provided False can therefore disable
    injection even when the environment enables it. When both are absent,
    injection is disabled (never silently enabled).
    """
    if explicit is not None:
        return bool(explicit)
    if env_value is None:
        return False
    return str(env_value).strip().lower() in ("1", "true", "yes", "on")


def inject_synthetic_slick(
    image,
    center,
    length_px: int = 40,
    width_px: int = 8,
    angle_deg: float = 30.0,
    darkness_db: float = -6.0,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Inject a smooth dark elliptical slick into a copy of ``image``.

    Parameters
    ----------
    image : np.ndarray
        Raw SAR backscatter values (dB: lower = darker). Never mutated.
    center : (row, col)
        Pixel centre of the injection (image coordinates; no fake geographic
        coordinates are invented here).
    length_px : int
        Major-axis length in pixels (demo parameter, NOT a validated oil-spill
        physical characteristic).
    width_px : int
        Minor-axis length in pixels (demo parameter).
    angle_deg : float
        Orientation of the major axis (degrees, counter-clockwise from the row
        axis).
    darkness_db : float
        Backscatter reduction applied inside the region (negative = darker).

    Returns
    -------
    (modified_copy, metadata)
        ``modified_copy`` preserves the input dtype; ``metadata`` records
        ``synthetic: True`` and every injection parameter for reproducibility.
    """
    source = np.asarray(image)
    if source.ndim != 2:
        raise ValueError("image must be a 2-D array.")
    if source.size == 0:
        raise ValueError("image must not be empty.")

    length = max(1.0, float(length_px))
    width = max(1.0, float(width_px))
    row0, col0 = float(center[0]), float(center[1])
    angle = float(angle_deg) * np.pi / 180.0

    rows = np.arange(source.shape[0], dtype=np.float64)[:, None]
    cols = np.arange(source.shape[1], dtype=np.float64)[None, :]

    dr = rows - row0
    dc = cols - col0
    # Rotate into the ellipse frame.
    dc_r = dc * np.cos(angle) + dr * np.sin(angle)
    dr_r = -dc * np.sin(angle) + dr * np.cos(angle)

    scaled = np.sqrt((dc_r / (width / 2.0)) ** 2 + (dr_r / (length / 2.0)) ** 2)
    weight = np.clip(1.0 - scaled, 0.0, 1.0)
    # Soft edges: Gaussian-blend the hard ellipse so the anomaly looks smooth
    # rather than artificially binary.
    sigma = max(1.0, min(length, width) / 8.0)
    weight = gaussian_filter(weight, sigma=float(sigma))

    out = source.astype(np.float64, copy=True)
    out = out + float(darkness_db) * weight

    if np.issubdtype(source.dtype, np.integer):
        out = np.round(out)
    result = out.astype(source.dtype, copy=False)

    metadata = {
        "synthetic": True,
        "injection": {
            "center": [int(round(row0)), int(round(col0))],
            "length_px": float(length),
            "width_px": float(width),
            "angle_deg": float(angle_deg),
            "darkness_db": float(darkness_db),
            "sigma_px": float(sigma),
        },
    }
    return result, metadata


def inject_geotiff_if_enabled(
    raster_path: str,
    scene_id: str,
    explicit: Optional[bool] = None,
    env_var: str = "INJECT_SYNTHETIC",
    length_px: int = 40,
    width_px: int = 8,
    angle_deg: float = 30.0,
    darkness_db: float = -6.0,
) -> Tuple[str, Dict[str, Any]]:
    """Read a GeoTIFF, inject a synthetic slick when enabled, write a NEW file.

    NEVER overwrites the original raster: the injected file is written as
    ``<stem>_synthetic.tif`` beside it and that path is returned (or the
    original path unchanged when injection is disabled).
    """
    import os

    import rasterio

    enabled = resolve_synthetic_enabled(explicit, os.environ.get(env_var))
    if not enabled:
        metadata = {"is_synthetic": False}
        return raster_path, metadata

    with rasterio.open(raster_path) as ds:
        image = ds.read()
        profile = ds.profile.copy()

    centre = (image.shape[1] // 2, image.shape[2] // 2)
    modified_band, inject_meta = inject_synthetic_slick(
        image[0],
        centre,
        length_px=length_px,
        width_px=width_px,
        angle_deg=angle_deg,
        darkness_db=darkness_db,
    )

    stem, ext = os.path.splitext(raster_path)
    out_path = f"{stem}_synthetic{ext}"
    with rasterio.open(out_path, "w", **profile) as ds_out:
        image[0] = modified_band
        ds_out.write(image)

    metadata = {"is_synthetic": True}
    metadata.update(inject_meta)
    return out_path, metadata


__all__ = [
    "resolve_synthetic_enabled",
    "inject_synthetic_slick",
    "inject_geotiff_if_enabled",
]
