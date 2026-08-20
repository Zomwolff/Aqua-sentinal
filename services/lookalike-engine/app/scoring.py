"""Heuristic confidence scoring for surviving SAR candidates (Step 5).

Runs AFTER Step 4 lookalike filtering. Only non-rejected candidates
(``possible_slick``) are scored; ship-shadow / calm-water candidates are passed
through with their Step 4 label and are never scored, though when one reaches
this function it still receives ``SHIP_SHADOW_PENALTY``.

This stage is heuristic. It never outputs ``confirmed`` / ``confirmed_oil`` /
``oil_confirmed``. ``possible_oil_spill`` means only:

    the candidate was not rejected (Step 4) and its confidence score is above
    the configured threshold.

It does NOT mean ground-truth oil confirmation.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

DARKNESS_WEIGHT = 0.30
TEXTURE_WEIGHT = 0.20
SHAPE_WEIGHT = 0.20
AREA_WEIGHT = 0.10
SHIP_SHADOW_PENALTY = -0.15
CONTEXT_WEIGHT = 0.20
CONFIDENCE_THRESHOLD = 0.5

# Component sub-heuristics (documented; not validated scientific thresholds).
TEXTURE_CONTRAST_SCALE = 1.0
AREA_SATURATION_PX = 50000.0

# Step 4 labels that are passed through without a confidence score.
PASS_THROUGH_LABELS = ("likely_ship_shadow", "likely_calm_water")

# Full allowed output-label set after Step 5.
ALLOWED_LABELS = (
    "likely_ship_shadow",
    "likely_calm_water",
    "possible_slick",
    "possible_oil_spill",
    "low_confidence",
)
FORBIDDEN_LABELS = ("confirmed", "confirmed_oil", "oil_confirmed")


def _fin(value: Any, fallback: float = 0.0) -> float:
    """Deterministic finite clamp: NaN/inf -> fallback, then clip to [0, 1]."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = fallback
    if not np.isfinite(v):
        v = fallback
    return float(np.clip(v, 0.0, 1.0))


def _darkness_score(texture_features: Dict[str, Any]) -> float:
    """Higher when raw mean backscatter is lower (darker), in dB units."""
    mean = texture_features.get("mean_backscatter")
    std = texture_features.get("std_backscatter")
    if mean is None:
        return 0.5
    m = float(mean)
    s = float(std) if std is not None else abs(m)
    s = max(abs(s), 1e-6)
    return float(np.clip(0.5 * (1.0 - np.tanh(m / s)), 0.0, 1.0))


def _texture_score(texture_features: Dict[str, Any]) -> float:
    """Structured texture: penalise uniformity but reward measurable contrast."""
    energy = texture_features.get("energy")
    contrast = texture_features.get("contrast")
    uniform_term = 1.0 - _fin(energy, fallback=1.0)
    contrast_term = 0.0 if contrast is None else float(contrast)
    contrast_term = _fin(contrast_term / TEXTURE_CONTRAST_SCALE, fallback=0.0)
    return float(np.clip(0.5 * uniform_term + 0.5 * contrast_term, 0.0, 1.0))


def _shape_score(candidate: Dict[str, Any]) -> float:
    """Uses Step 4 shape descriptors (solidity) when the caller provides them."""
    shape = candidate.get("shape_features")
    if isinstance(shape, dict) and shape.get("solidity") is not None:
        return _fin(shape.get("solidity"))
    return 0.5


def _area_score(candidate: Dict[str, Any]) -> float:
    pixel_count = candidate.get("pixel_count")
    if pixel_count is None:
        return 0.5
    return float(np.clip(float(pixel_count) / AREA_SATURATION_PX, 0.0, 1.0))


def score_candidate(
    candidate: Dict[str, Any],
    texture_features: Dict[str, Any],
    context_score: float = 0.5,
) -> Dict[str, Any]:
    """Score one surviving candidate.

    Parameters
    ----------
    candidate : dict
        Candidate record; optional ``classification_label`` (Step 4 result),
        optional ``shape_features`` (from Step 4 ``compute_shape_descriptors``),
        ``pixel_count``.
    texture_features : dict
        Output of ``texture.compute_glcm_features``.
    context_score : float
        Reserved for the Step 6 evidence-fusion/AIS context stage. Defaults to
        a neutral 0.5; clipped to [0, 1]. No vessel/AIS context is computed here.

    Returns
    -------
    dict with:
        confidence (clipped [0, 1], never NaN/inf),
        classification_label (one of ALLOWED_LABELS),
        score_components (darkness, texture, shape, area, context,
                          ship_shadow_penalty_applied).
    """
    darkness = _darkness_score(texture_features)
    texture = _texture_score(texture_features)
    shape = _shape_score(candidate)
    area = _area_score(candidate)
    context = _fin(context_score, fallback=0.5)

    label = candidate.get("classification_label")
    ship_shadow_penalty = label == "likely_ship_shadow"

    confidence = (
        DARKNESS_WEIGHT * darkness
        + TEXTURE_WEIGHT * texture
        + SHAPE_WEIGHT * shape
        + AREA_WEIGHT * area
        + CONTEXT_WEIGHT * context
        + (SHIP_SHADOW_PENALTY if ship_shadow_penalty else 0.0)
    )
    confidence = float(np.clip(confidence, 0.0, 1.0))

    if label in PASS_THROUGH_LABELS:
        classification_label = label
        confidence = float(
            np.clip(confidence, 0.0, 1.0)
        )  # pass-through still finite/clamped
    elif confidence > CONFIDENCE_THRESHOLD:
        classification_label = "possible_oil_spill"
    else:
        classification_label = "low_confidence"

    # Defensive: never allow a ground-truth label out.
    if classification_label in FORBIDDEN_LABELS:
        classification_label = "low_confidence"

    return {
        "confidence": confidence,
        "classification_label": classification_label,
        "score_components": {
            "darkness": darkness,
            "texture": texture,
            "shape": shape,
            "area": area,
            "context": context,
            "ship_shadow_penalty_applied": ship_shadow_penalty,
        },
    }