"""Connected-component extraction and GeoJSON polygonization of SAR spill masks.

STEP 3 polygonization. Converts a cleaned binary mask into spill candidates.

This module is deliberately DB/network free: ``extract_candidates`` performs pure
geometry work only. Computing the physical area (m²) requires PostGIS geography
and is done by the caller (the worker) through ``area_m2_from_geometry``, so the
candidate's ``area_m2`` is None until that step stamps it.

Coordinate convention (docs/spatial.md): all output coordinates are in
(longitude, latitude) order with EPSG:4326. The affine transform maps
``(col, row)`` pixel indices to geographic X (longitude) / Y (latitude), so the
ordering is enforced at the source and never reversed.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
from affine import Affine
from shapely.geometry import Polygon as ShapelyPolygon
from shapely.geometry import mapping
from skimage.measure import find_contours, label, regionprops

from app.morphology import estimate_min_area_px

log = logging.getLogger(__name__)

# NOTE: geometry validity/repair uses shapely topology only. Physical area is
# never derived from shapely (the planar coordinate values are degrees) — that
# is left to the caller's PostGIS geography computation.


def _as_affine(transform) -> Affine:
    """Normalise a rasterio/affine transform into an ``Affine``.

    Accepts an ``Affine`` instance or a coefficient tuple. rasterio emits
    9-element tuples (the full 3x3 matrix); the 2-D coefficients are the first
    six entries, which is what ``Affine`` consumes.
    """
    if isinstance(transform, Affine):
        return transform
    coeffs = tuple(transform)
    if len(coeffs) == 9:
        coeffs = coeffs[:6]
    if len(coeffs) != 6:
        raise ValueError(
            "transform must be an affine.Affine or a 6/9-element coefficient tuple."
        )
    return Affine(*coeffs)


def _pixel_to_geo(affine: Affine, row: float, col: float) -> (float, float):
    """Map a pixel (row, col) to (longitude, latitude)."""
    lon = affine.a * col + affine.b * row + affine.c
    lat = affine.d * col + affine.e * row + affine.f
    return float(lon), float(lat)


def _validate_inputs(mask, min_area_m2, pixel_size_m, transform) -> None:
    arr = np.asarray(mask)
    if arr.ndim != 2:
        raise ValueError("mask must be a 2-D array.")
    if arr.size == 0:
        raise ValueError("mask must not be empty.")
    if not np.issubdtype(arr.dtype, np.bool_):
        raise ValueError("mask must be a boolean array.")
    # Surface a bad threshold before any component work.
    estimate_min_area_px(pixel_size_m=pixel_size_m, min_area_m2=min_area_m2)
    _as_affine(transform)


def _rings_for_region(region, affine: Affine) -> List[List[List[float]]]:
    """Return GeoJSON rings (each ``[lon, lat]``) for a labeled region.

    Contours come from ``skimage.measure.find_contours`` in ``(row, col)``
    order. The region image is padded by one background pixel so a component
    that touches its own bounding-box edge still yields a closed contour.
    Contour sub-pixel coordinates are mapped onto full-image pixels before the
    affine transform (col -> longitude, row -> latitude) is applied.
    """
    padded = np.pad(region.image, 1, mode="constant", constant_values=False)
    contours = find_contours(padded.astype(np.float64), level=0.5)

    rings: List[List[List[float]]] = []
    for contour in contours:
        points: List[List[float]] = []
        for local_row, local_col in contour:
            row = local_row - 1 + region.bbox[0]
            col = local_col - 1 + region.bbox[1]
            lon, lat = _pixel_to_geo(affine, row, col)
            points.append([lon, lat])
        if len(points) < 3:
            continue
        if points[0] != points[-1]:
            points.append(points[0])
        if len(points) >= 4:
            rings.append(points)
    return rings


def _geojson_polygon(rings: Sequence[List[List[float]]]) -> Optional[Dict[str, Any]]:
    """Build a valid GeoJSON Polygon from rings, or None if it cannot be valid.

    The longest ring is the exterior shell; any other ring nested inside it is a
    hole. Invalid geometry is repaired with a zero-width buffer; if that fails
    the candidate is explicitly dropped (None) instead of emitting malformed
    GeoJSON.
    """
    if not rings:
        return None
    ordered = sorted(rings, key=len, reverse=True)
    shell = ordered[0]
    shell_poly = ShapelyPolygon(shell)

    holes: List[List[List[float]]] = []
    for ring in ordered[1:]:
        ring_poly = ShapelyPolygon(ring)
        if ring_poly.is_empty:
            continue
        if shell_poly.contains(ring_poly.representative_point()):
            holes.append(ring)

    polygon = ShapelyPolygon(shell, holes) if holes else ShapelyPolygon(shell)
    if not polygon.is_valid:
        repaired = polygon.buffer(0)
        if repaired.is_empty or not repaired.is_valid:
            return None
        polygon = repaired
    return mapping(polygon)


def _centroid(region, affine: Affine) -> Dict[str, Any]:
    row, col = float(region.centroid[0]), float(region.centroid[1])
    lon, lat = _pixel_to_geo(affine, row, col)
    return {"type": "Point", "coordinates": [lon, lat]}


def extract_candidates(
    mask,
    transform,
    min_area_m2,
    pixel_size_m,
) -> List[Dict[str, Any]]:
    """Extract spill candidates from a cleaned binary mask.

    Pure geometry step with no DB/network access.

    Parameters
    ----------
    mask : np.ndarray (bool)
        Cleaned binary mask; True pixels belong to potential slicks.
    transform : affine.Affine or tuple
        Pixel-to-geographic transform. Output coordinates are always
        (longitude, latitude).
    min_area_m2 : float
        Physical minimum spill area in m², from validation/configuration. A
        component below this threshold is not a candidate. Must be supplied
        explicitly (see ``estimate_min_area_px``).
    pixel_size_m : float
        Ground-range pixel size used to convert ``min_area_m2`` into pixels.

    Returns
    -------
    list of dict, one per surviving connected component:
        candidate_id, geometry (GeoJSON Polygon, EPSG:4326, lon/lat),
        area_m2 (None until the caller resolves it via the PostGIS geography
        path through ``area_m2_from_geometry``), centroid (GeoJSON Point),
        pixel_count.
    """
    _validate_inputs(mask, min_area_m2, pixel_size_m, transform)
    affine = _as_affine(transform)
    min_area_px = estimate_min_area_px(
        pixel_size_m=pixel_size_m,
        min_area_m2=min_area_m2,
    )

    values = np.asarray(mask).astype(bool)
    labels = label(values, connectivity=2)

    candidates: List[Dict[str, Any]] = []
    for region in regionprops(labels):
        pixel_count = int(region.area)
        if pixel_count < min_area_px:
            continue

        rings = _rings_for_region(region, affine)
        geojson = _geojson_polygon(rings)
        if geojson is None:
            log.warning(
                "Dropping spill candidate with invalid polygon geometry "
                "pixel_count=%d",
                pixel_count,
            )
            continue

        candidates.append(
            {
                "candidate_id": uuid.uuid4(),
                "geometry": geojson,
                "area_m2": None,
                "centroid": _centroid(region, affine),
                "pixel_count": pixel_count,
            }
        )
    return candidates