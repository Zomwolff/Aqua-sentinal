import sys
from pathlib import Path

import numpy as np
import pytest

SERVICE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_ROOT))

from app.scoring import (
    ALLOWED_LABELS,
    CONFIDENCE_THRESHOLD,
    SHIP_SHADOW_PENALTY,
    score_candidate,
)

FORBIDDEN = ("confirmed", "confirmed_oil", "oil_confirmed")


def _strong_texture():
    return {
        "contrast": 0.8,
        "homogeneity": 0.4,
        "energy": 0.2,
        "correlation": 0.4,
        "mean_backscatter": -10.0,
        "std_backscatter": 1.0,
    }


def _neutral_texture():
    return {
        "contrast": 0.3,
        "homogeneity": 0.6,
        "energy": 0.6,
        "correlation": 0.3,
        "mean_backscatter": -5.0,
        "std_backscatter": 2.0,
    }


def test_strong_slick_like_candidate_is_possible_oil_spill():
    candidate = {
        "pixel_count": 40000,
        "classification_label": "possible_slick",
        "shape_features": {"solidity": 1.0, "elongation": 1.1},
    }
    result = score_candidate(candidate, _strong_texture(), context_score=0.5)

    assert result["confidence"] > CONFIDENCE_THRESHOLD
    assert result["classification_label"] == "possible_oil_spill"
    assert result["score_components"]["ship_shadow_penalty_applied"] is False


def test_ship_shadow_receives_penalty_and_stays_below_threshold():
    # Components designed so the base score stays below the possible-oil
    # threshold; the penalty drops it further and the label passes through.
    texture = {
        "contrast": 0.2,
        "homogeneity": 0.5,
        "energy": 0.8,
        "correlation": 0.3,
        "mean_backscatter": -1.0,
        "std_backscatter": 1.0,
    }
    candidate = {
        "pixel_count": 500,
        "classification_label": "likely_ship_shadow",
        "shape_features": {"solidity": 0.5, "elongation": 20.0},
    }
    result = score_candidate(candidate, texture, context_score=0.5)
    assert result["score_components"]["ship_shadow_penalty_applied"] is True
    assert result["confidence"] == pytest.approx(
        result["score_components"]["darkness"] * 0.30
        + result["score_components"]["texture"] * 0.20
        + result["score_components"]["shape"] * 0.20
        + result["score_components"]["area"] * 0.10
        + result["score_components"]["context"] * 0.20
        + SHIP_SHADOW_PENALTY,
        rel=1e-9,
    )
    assert result["confidence"] < CONFIDENCE_THRESHOLD
    assert result["classification_label"] == "likely_ship_shadow"


def test_randomized_inputs_stay_in_range_and_labels_are_whitelisted():
    rng = np.random.default_rng(99)
    for _ in range(500):
        texture = {
            "contrast": float(rng.uniform(0, 5)),
            "homogeneity": float(rng.uniform(0, 1)),
            "energy": float(rng.uniform(0, 1)),
            "correlation": float(rng.uniform(-1, 1)),
            "mean_backscatter": float(rng.uniform(-20, 5)),
            "std_backscatter": float(rng.uniform(0, 4)),
        }
        candidate = {
            "pixel_count": int(rng.integers(0, 100000)),
            "classification_label": rng.choice(
                ["possible_slick", "likely_ship_shadow", "likely_calm_water"]
            ),
            "shape_features": {"solidity": float(rng.uniform(0, 1))},
        }
        result = score_candidate(candidate, texture, context_score=float(rng.uniform(0, 1)))
        assert 0.0 <= result["confidence"] <= 1.0
        assert result["classification_label"] in ALLOWED_LABELS
        assert result["classification_label"] not in FORBIDDEN
        for v in result["score_components"].values():
            assert isinstance(v, bool) or (isinstance(v, float) and 0.0 <= v <= 1.0)


def test_nan_and_invalid_components_are_sanitized():
    texture = {
        "contrast": float("nan"),
        "homogeneity": float("inf"),
        "energy": None,
        "correlation": float("nan"),
        "mean_backscatter": float("inf"),
        "std_backscatter": -1.0,
    }
    candidate = {
        "pixel_count": 100,
        "classification_label": "possible_slick",
        "shape_features": {"solidity": float("nan")},
    }
    result = score_candidate(candidate, texture, context_score=float("nan"))
    assert np.isfinite(result["confidence"])
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["classification_label"] in ALLOWED_LABELS


def test_context_score_is_clipped():
    candidate = {"pixel_count": 100, "classification_label": "possible_slick"}
    low = score_candidate(candidate, _neutral_texture(), context_score=-3.0)
    high = score_candidate(candidate, _neutral_texture(), context_score=7.0)
    assert low["score_components"]["context"] == 0.0
    assert high["score_components"]["context"] == 1.0


def test_threshold_boundary_is_low_confidence():
    # Every component contributes exactly 0.5 -> confidence == 0.5.
    neutral = {
        "contrast": 0.0,
        "homogeneity": 1.0,
        "energy": 0.0,
        "correlation": 1.0,
        "mean_backscatter": 0.0,
        "std_backscatter": 0.5,
    }
    candidate = {
        "pixel_count": int(0.5 * 50000),
        "classification_label": "possible_slick",
        "shape_features": {"solidity": 0.5},
    }
    result = score_candidate(candidate, neutral, context_score=0.5)
    assert result["confidence"] == pytest.approx(CONFIDENCE_THRESHOLD, abs=1e-6)
    # confidence <= threshold must NOT be possible_oil_spill.
    assert result["classification_label"] == "low_confidence"


def test_never_outputs_confirmed_ground_truth():
    for _ in range(200):
        result = score_candidate(
            {"pixel_count": 100, "classification_label": "possible_slick"},
            _neutral_texture(),
            context_score=0.5,
        )
        assert "confirm" not in result["classification_label"].lower()


def test_scoring_is_deterministic():
    candidate = {"pixel_count": 2000, "classification_label": "possible_slick",
                 "shape_features": {"solidity": 0.9}}
    a = score_candidate(candidate, _strong_texture(), context_score=0.6)
    b = score_candidate(candidate, _strong_texture(), context_score=0.6)
    assert a == b