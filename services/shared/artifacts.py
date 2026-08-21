"""Scene-level processed-SAR artifact storage for the spill pipeline.

Decision (Step 4): raster pixels are NEVER stored in PostgreSQL and never one
artifact per candidate. Instead, each processed SAR scene produces exactly one
artifact bundle on a shared Docker volume, retrievable by ``scene_id``:

    <ARTIFACT_ROOT>/<scene_safe_id>/
        metadata.json            affine transform, shape, crs, scene id
        raw_image.npy            pre-despeckle SAR backscatter values (float)
        filtered_image.npy       Lee-filtered backscatter values (float)
        cleaned_mask.npy         morphologically cleaned dark-candidate mask
        bright_target_mask.npy   high-backscatter targets (bool)

No database index is required: the scene_id is the key (filesystem directory
name). ``candidate_id -> scene_id -> artifact`` is the mapping used by the
lookalike worker.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Sequence

import numpy as np
from affine import Affine
from skimage.io import imsave

_SAFE_RE = re.compile(r"[^A-Za-z0-9_.=-]")


def scene_artifact_path(root: str, scene_id: str) -> str:
    """Directory for a scene's artifacts (filesystem name is the scene index)."""
    safe = _SAFE_RE.sub("_", str(scene_id))
    return os.path.join(root, safe)


def save_scene_artifact(
    root: str,
    scene_id: str,
    *,
    raw_image: Optional[np.ndarray] = None,
    filtered_image: np.ndarray,
    cleaned_mask: np.ndarray,
    bright_target_mask: np.ndarray,
    affine: Sequence[float],
    shape: Sequence[int],
    crs: Optional[str] = None,
) -> str:
    """Persist one scene's processed-artifact bundle; returns the directory.

    ``raw_image`` (pre-despeckle backscatter) is stored when provided, so a
    scene artifact always retains the texture detail that the Lee filter later
    smooths away (required by Step 5 GLCM texture analysis).
    """
    path = scene_artifact_path(root, scene_id)
    os.makedirs(path, exist_ok=True)

    if raw_image is not None:
        np.save(os.path.join(path, "raw_image.npy"), np.asarray(raw_image))
        # Save preview
        img = np.asarray(raw_image)
        v_min, v_max = np.nanpercentile(img, (2, 98))
        if v_max > v_min:
            img_norm = np.clip((img - v_min) / (v_max - v_min), 0, 1)
            imsave(os.path.join(path, "raw_image.png"), (img_norm * 255).astype(np.uint8), check_contrast=False)

    np.save(os.path.join(path, "filtered_image.npy"), np.asarray(filtered_image))
    img = np.asarray(filtered_image)
    v_min, v_max = np.nanpercentile(img, (2, 98))
    if v_max > v_min:
        img_norm = np.clip((img - v_min) / (v_max - v_min), 0, 1)
        imsave(os.path.join(path, "filtered_image.png"), (img_norm * 255).astype(np.uint8), check_contrast=False)

    np.save(os.path.join(path, "cleaned_mask.npy"), np.asarray(cleaned_mask))
    imsave(os.path.join(path, "cleaned_mask.png"), (np.asarray(cleaned_mask) * 255).astype(np.uint8), check_contrast=False)

    np.save(os.path.join(path, "bright_target_mask.npy"), np.asarray(bright_target_mask))
    imsave(os.path.join(path, "bright_target_mask.png"), (np.asarray(bright_target_mask) * 255).astype(np.uint8), check_contrast=False)

    coeffs = [float(v) for v in list(affine)[:6]]
    metadata = {
        "scene_id": str(scene_id),
        "crs": crs,
        "shape": [int(shape[0]), int(shape[1])],
        "affine": coeffs,
    }
    with open(os.path.join(path, "metadata.json"), "w") as handle:
        json.dump(metadata, handle)
    return path


def load_scene_artifact(root: str, scene_id: str) -> Optional[Dict[str, Any]]:
    """Load a scene's artifact bundle, or None when it does not exist.

    Returns a dict with keys: filtered_image, cleaned_mask, bright_target_mask,
    raw_image (None when the raw file predates Step 5), metadata (parsed
    metadata.json).
    """
    path = scene_artifact_path(root, scene_id)
    if not os.path.isdir(path):
        return None
    meta_path = os.path.join(path, "metadata.json")
    if not os.path.isfile(meta_path):
        return None
    with open(meta_path) as handle:
        metadata = json.load(handle)
    raw_path = os.path.join(path, "raw_image.npy")
    return {
        "metadata": metadata,
        "raw_image": np.load(raw_path) if os.path.isfile(raw_path) else None,
        "filtered_image": np.load(os.path.join(path, "filtered_image.npy")),
        "cleaned_mask": np.load(os.path.join(path, "cleaned_mask.npy")),
        "bright_target_mask": np.load(os.path.join(path, "bright_target_mask.npy")),
    }


def _as_affine(coefficients: Sequence[float]) -> Affine:
    coeffs = [float(v) for v in coefficients]
    if len(coeffs) == 9:
        coeffs = coeffs[:6]
    if len(coeffs) != 6:
        raise ValueError("affine must be a 6-element (or 9-element) coefficient list.")
    return Affine(*coeffs)


def geometry_to_pixel_bbox(
    geometry: Dict[str, Any],
    affine: Sequence[float],
    *,
    padding: int = 0,
    shape: Optional[Sequence[int]] = None,
) -> tuple:
    """Map a lon/lat GeoJSON polygon to a pixel crop box ``(r0, r1, c0, c1)``.

    Uses the inverse affine transform; world X (longitude) maps to column and
    world Y (latitude) to row — never the reverse. Coordinates are clamped to
    the raster shape when supplied.
    """
    transform = _as_affine(affine)
    inv = ~transform

    points = [point for ring in geometry.get("coordinates", []) for point in ring]
    if not points:
        raise ValueError("geometry has no coordinates to map.")

    cols: list[float] = []
    rows: list[float] = []
    for lon, lat in points:
        col, row = inv * (float(lon), float(lat))
        cols.append(col)
        rows.append(row)

    r0 = int(np.floor(min(rows))) - int(padding)
    r1 = int(np.ceil(max(rows))) + int(padding)
    c0 = int(np.floor(min(cols))) - int(padding)
    c1 = int(np.ceil(max(cols))) + int(padding)

    if shape is not None:
        height, width = int(shape[0]), int(shape[1])
        r0 = max(0, r0)
        r1 = min(height, r1)
        c0 = max(0, c0)
        c1 = min(width, c1)
        if r1 <= r0 or c1 <= c0:
            raise ValueError("geometry maps outside the raster extent.")

    return (r0, r1, c0, c1)


def crop_region(array: np.ndarray, bbox: tuple) -> np.ndarray:
    """Slice ``array[r0:r1, c0:c1]`` for a pixel bbox."""
    r0, r1, c0, c1 = bbox
    return np.asarray(array)[r0:r1, c0:c1]