"""
Step 6 Part A: Observation Likelihood — observation_likelihood.py

Defines the statistical observation-error model that converts Step 5
footprint-comparison metrics into a log-likelihood L(y | θ).

LIKELIHOOD MODEL
================
The v1 model assumes conditional independence between centroid and area errors.

Centroid term:
    The 2D centroid residual (dx_m, dy_m) is modeled as a 2D isotropic Gaussian:
        dx ~ N(0, σ_c²), dy ~ N(0, σ_c²), independent
        log L_centroid = -0.5*(dx²+dy²)/σ_c²  - log(2π σ_c²)

    We use delta_lon_m and delta_lat_m from FootprintComparisonResult directly.
    This is the correct formulation for a 2D position residual.
    Do NOT substitute centroid_distance_m into a 1D Gaussian.

Area term:
    Area ratios are multiplicative; the log-area ratio is additive:
        r_A = log(A_sim / A_obs)
        r_A ~ N(0, σ_A_log²)
        log L_area = -0.5*(r_A/σ_A_log)² - log(√(2π) σ_A_log)

Combined:
    log L(y | θ) = log L_centroid + log L_area
    (IoU term added only if config.use_iou=True and iou is not None)

UNCERTAINTY PARAMETERS
======================
    sigma_centroid_m:  1σ centroid error in metres (default 5000 m)
    sigma_area_log:    1σ log-area ratio (default 1.0 — O(1) area factor uncertainty)

STATUS: These are v1 modeling assumptions, NOT empirically calibrated.
They should be calibrated against known historical spill events in a later step.

PRIOR
=====
log_prior_uniform() returns 0.0 — a constant log-prior representing equal
weight over all candidates in degree-space. This is a v1 approximation;
degree-space uniformity is not exactly uniform over surface area.

JACOBIAN NOTE
=============
The proposal_density stored by Step 4 (candidate_generation.py) is the KDE
density in scaled coordinates q̃(T(θ)) where T divides elapsed_h by H.
For normalized importance weights, the Jacobian factor 1/H is constant across
all candidates and cancels in normalization. Therefore log_proposal = log(q̃)
is used directly, with no explicit Jacobian correction.

SCIENTIFIC LIMITATIONS
======================
1. Backward candidate generation excludes diffusion, Fay spreading, weathering.
2. Historical forcing is a single spatial point (no spatial gradient).
3. Observed footprint has centroid + area only (no SAR polygon in v1).
4. sigma_centroid_m and sigma_area_log are v1 modeling assumptions, not calibrated.
5. Uniform prior assigns equal density in degree-space, not exact surface area.
6. Importance sampling with finite N is an approximation; ESS quantifies quality.
7. A high posterior weight does NOT establish historical truth.
8. The KDE proposal density is in scaled space; Jacobian cancels in normalization.

WHAT THIS MODULE DOES NOT DO
=============================
- Does NOT compute posterior probabilities
- Does NOT implement Bayesian inference (that is bayesian_inference.py)
- Does NOT use AIS, ML, or vessel attribution
- Does NOT modify any existing file
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from app.footprint_comparison import FootprintComparisonResult
from app.candidate_generation import CandidateSourceHypothesis


# ---------------------------------------------------------------------------
# LikelihoodConfig
# ---------------------------------------------------------------------------

@dataclass
class LikelihoodConfig:
    """
    Configurable observation/model uncertainty parameters.

    IMPORTANT: These are v1 modeling assumptions, NOT empirically calibrated.
    sigma_centroid_m and sigma_area_log should be calibrated against known
    historical spill events before using this system for operational attribution.

    Fields
    ------
    sigma_centroid_m : float
        1σ centroid position uncertainty in metres.
        Default 5000 m = 5 km (O(km) typical for SAR centroid error + model error).
    sigma_area_log : float
        1σ log-area ratio (dimensionless).
        Default 1.0: allows ~e¹ ≈ 2.7× area mismatch within 1σ.
    use_iou : bool
        If True and iou is not None in comparison, include an IoU likelihood term.
        Default False — in v1 observed polygon is unavailable.
    sigma_iou : float
        1σ IoU residual for the optional IoU term.
        Only used if use_iou=True and iou is not None.
    """
    sigma_centroid_m: float = 5_000.0
    sigma_area_log:   float = 1.0
    use_iou:          bool  = False
    sigma_iou:        float = 0.2

    def __post_init__(self):
        if self.sigma_centroid_m <= 0:
            raise ValueError(
                f"sigma_centroid_m={self.sigma_centroid_m} must be positive"
            )
        if self.sigma_area_log <= 0:
            raise ValueError(
                f"sigma_area_log={self.sigma_area_log} must be positive"
            )
        if self.sigma_iou <= 0:
            raise ValueError(
                f"sigma_iou={self.sigma_iou} must be positive"
            )


# ---------------------------------------------------------------------------
# Internal likelihood components
# ---------------------------------------------------------------------------

def _log_likelihood_centroid(
    dx_m: float,
    dy_m: float,
    sigma_c: float,
) -> float:
    """
    2D isotropic Gaussian log-likelihood for centroid residual.

    Model:
        dx ~ N(0, σ_c²), dy ~ N(0, σ_c²), independent

    log L_centroid = -0.5*(dx² + dy²)/σ_c² - log(2π σ_c²)

    Parameters
    ----------
    dx_m : float
        East-west signed offset in metres (delta_lon_m from FootprintComparisonResult).
    dy_m : float
        North-south signed offset in metres (delta_lat_m from FootprintComparisonResult).
    sigma_c : float
        1σ centroid uncertainty in metres.

    Returns
    -------
    float
        Log-likelihood contribution from centroid residual.
    """
    sigma2 = sigma_c * sigma_c
    return -0.5 * (dx_m * dx_m + dy_m * dy_m) / sigma2 - math.log(2.0 * math.pi * sigma2)


def _log_likelihood_area(
    A_sim: float,
    A_obs: float,
    sigma_A_log: float,
) -> float:
    """
    Log-normal log-likelihood for area ratio.

    Model:
        r_A = log(A_sim / A_obs)  [log-area ratio]
        r_A ~ N(0, σ_A_log²)

    log L_area = -0.5*(r_A/σ_A_log)² - log(√(2π) σ_A_log)

    Parameters
    ----------
    A_sim : float
        Simulated spill area in m². Guarded against zero by epsilon=1.0.
    A_obs : float
        Observed spill area in m². Guarded against zero by epsilon=1.0.
    sigma_A_log : float
        1σ log-area ratio (dimensionless).

    Returns
    -------
    float
        Log-likelihood contribution from area ratio.
    """
    eps = 1.0
    r_A = math.log(max(A_sim, eps) / max(A_obs, eps))
    return (
        -0.5 * (r_A / sigma_A_log) ** 2
        - math.log(math.sqrt(2.0 * math.pi) * sigma_A_log)
    )


def _log_likelihood_iou(
    iou: float,
    sigma_iou: float,
) -> float:
    """
    Optional Gaussian log-likelihood for IoU residual.

    Model:
        r_iou = 1.0 - iou  [perfect overlap = 0 residual]
        r_iou ~ N(0, σ_iou²)

    log L_iou = -0.5*(r_iou/σ_iou)² - log(√(2π) σ_iou)

    Parameters
    ----------
    iou : float
        IoU value in [0, 1].
    sigma_iou : float
        1σ IoU uncertainty.
    """
    r_iou = 1.0 - iou
    return (
        -0.5 * (r_iou / sigma_iou) ** 2
        - math.log(math.sqrt(2.0 * math.pi) * sigma_iou)
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def log_likelihood(
    comparison: FootprintComparisonResult,
    config: LikelihoodConfig,
) -> float:
    """
    Total log-likelihood L(y | θ) for one candidate.

    Combines centroid and area terms under conditional-independence assumption.
    IoU term added only if config.use_iou=True and comparison.iou is not None.

    Parameters
    ----------
    comparison : FootprintComparisonResult
        Output of compare_footprints() from Step 5.
        Uses: delta_lon_m, delta_lat_m, simulated_area_m2, observed_area_m2, iou.
    config : LikelihoodConfig
        Uncertainty parameters.

    Returns
    -------
    float
        log L(y | θ). NOT a probability — call math.exp() to get L(y|θ).
        Returns -inf if inputs are non-finite.
    """
    if not math.isfinite(comparison.delta_lat_m) or not math.isfinite(comparison.delta_lon_m):
        return -math.inf

    ll = 0.0

    # 2D Gaussian centroid term
    ll += _log_likelihood_centroid(
        dx_m=comparison.delta_lon_m,
        dy_m=comparison.delta_lat_m,
        sigma_c=config.sigma_centroid_m,
    )

    # Log-normal area term
    ll += _log_likelihood_area(
        A_sim=comparison.simulated_area_m2,
        A_obs=comparison.observed_area_m2,
        sigma_A_log=config.sigma_area_log,
    )

    # Optional IoU term (excluded in v1 when polygon unavailable)
    if config.use_iou and comparison.iou is not None:
        ll += _log_likelihood_iou(comparison.iou, config.sigma_iou)

    return ll


def log_prior_uniform(candidate: CandidateSourceHypothesis) -> float:
    """
    Uniform prior log-probability: returns 0.0 (log of a constant).

    ASSUMPTION (v1):
    Equal prior weight in degree-space over the candidate support region.
    This is NOT exactly uniform over spherical surface area — degrees of
    longitude are shorter at higher latitudes. This is the v1 approximation.

    The function signature accepts a candidate argument to allow future
    replacement by an informative prior (e.g., shipping-lane density,
    meteorological plausibility) without changing calling code.

    Returns
    -------
    float
        0.0 — the log of a constant prior density. Cancels in normalization.
    """
    return 0.0
