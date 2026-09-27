"""
Step 6 Part B: Bayesian Importance Sampling — bayesian_inference.py

Implements importance-sampling approximation to the posterior distribution
over candidate source hypotheses θ = (source_lat, source_lon, release_time).

IMPORTANCE SAMPLING FORMULA
============================
For each candidate i:
    log w̃ᵢ = log L(y|θᵢ) + log p(θᵢ) - log q̃(θᵢ)

where:
    L(y|θᵢ)  = observation likelihood from observation_likelihood.py
    p(θᵢ)    = prior (uniform in v1, log p = 0)
    q̃(θᵢ)   = KDE proposal density stored by Step 4 (CandidateSourceHypothesis.proposal_density)

Normalized weights:
    wᵢ = exp(log w̃ᵢ - log Σⱼ exp(log w̃ⱼ))
    Σᵢ wᵢ = 1

JACOBIAN NOTE
=============
The proposal_density q̃ stored by Step 4 is the KDE density in scaled
coordinates (lat, lon, elapsed_h/H). The physical-space density is q̃/H.
Since H is constant across all candidates in the same inference run, the
factor 1/H cancels in normalized weights:
    wᵢ ∝ L p / (q̃/H) = L p H / q̃
Since H is constant, the normalized wᵢ are identical to using q̃ directly.
Therefore log_proposal_i = log(proposal_density_i) with NO Jacobian correction.

FAILURE HANDLING
================
If a candidate failed in Step 5 (success=False):
    log_likelihood_i = -inf → log weight = -inf → normalized weight = 0
The candidate's identity is preserved in the output.

If proposal_density_i is None or ≤ 0:
    log_proposal_i = -inf → log weight = -inf → normalized weight = 0

FALLBACK
========
If ALL candidates have zero normalized weight (e.g., all step5 failures),
a uniform equal-weight fallback is applied and recorded in inference_metadata.

POSTERIOR APPROXIMATION NOTE
==============================
This is a discrete Monte Carlo approximation to the continuous posterior.
With N candidates, the quality is measured by ESS (effective sample size).
Low ESS indicates that a few candidates dominate the posterior — the proposal
may be poorly aligned with the likelihood.

SCIENTIFIC LIMITATIONS
======================
1. Backward candidate generation excludes diffusion, Fay spreading, weathering.
2. Historical forcing is a single spatial point.
3. Observed footprint has centroid + area only (no SAR polygon in v1).
4. Likelihood parameters are v1 modeling assumptions, not empirically calibrated.
5. Uniform prior assigns equal density in degree-space, not exact surface area.
6. Importance sampling with finite N is an approximation.
7. A high posterior weight does NOT establish historical truth.
8. Posterior quality depends on the proposal covering plausible hypotheses.

WHAT THIS MODULE DOES NOT DO
=============================
- Does NOT implement MCMC, SMC, or variational inference
- Does NOT use AIS, ML, or vessel attribution
- Does NOT modify any existing file
- Does NOT produce a continuous posterior — only a weighted particle set
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.observation_likelihood import (
    LikelihoodConfig,
    log_likelihood,
    log_prior_uniform,
)
from app.forward_adapter import CandidateSimulationResult
from app.candidate_generation import CandidateSourceHypothesis


# ---------------------------------------------------------------------------
# BayesianInferenceResult
# ---------------------------------------------------------------------------

@dataclass
class BayesianInferenceResult:
    """
    Approximate posterior over candidate source hypotheses via importance sampling.

    SCIENTIFIC DISCLAIMER:
    This is an approximate posterior represented by N weighted particles.
    It is NOT an exact continuous posterior distribution.
    The MAP candidate has the highest posterior weight — it is NOT the confirmed
    source and should NOT be described as the true origin.
    Posterior quality depends on the proposal's coverage of plausible hypotheses
    and on the accuracy of the observation model parameters.

    Fields
    ------
    candidate_ids:          Candidate identifiers (same order as input).
    source_lats/lons:       Candidate source locations.
    release_times:          Candidate release times.
    log_likelihoods:        log L(y|θᵢ); -inf for failed/unevaluated.
    log_priors:             log p(θᵢ); 0.0 for uniform prior.
    log_proposals:          log q̃(θᵢ); -inf if proposal_density ≤ 0 or None.
    log_weights_unnorm:     log w̃ᵢ = ll + lp - lq; -inf for zero-weight.
    normalized_weights:     wᵢ = w̃ᵢ / Σ w̃; sums to 1.0.
    effective_sample_size:  ESS = 1/Σwᵢ²; diagnostic for IS quality.
    ess_ratio:              ESS / N_total.
    map_candidate_id:       candidate_id with highest normalized weight.
    map_candidate_weight:   Normalized weight of MAP candidate.
    n_total:                Total number of candidates.
    n_evaluated:            Candidates with success=True in Step 5.
    n_zero_weight:          Candidates with zero normalized weight.
    spatial_posterior_summary:  Weighted mean/std of lat/lon.
    temporal_posterior_summary: Weighted quantiles over release_time.
    inference_metadata:     Algorithm details and any warnings.
    """
    candidate_ids:             List[int]
    source_lats:               List[float]
    source_lons:               List[float]
    release_times:             List[datetime]
    log_likelihoods:           List[float]
    log_priors:                List[float]
    log_proposals:             List[float]
    log_weights_unnorm:        List[float]
    normalized_weights:        List[float]
    effective_sample_size:     float
    ess_ratio:                 float
    map_candidate_id:          int
    map_candidate_weight:      float
    n_total:                   int
    n_evaluated:               int
    n_zero_weight:             int
    spatial_posterior_summary: Dict[str, Any]
    temporal_posterior_summary: Dict[str, Any]
    inference_metadata:        Dict[str, Any]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _logsumexp(log_values: np.ndarray) -> float:
    """
    Numerically stable log-sum-exp.

    Handles arrays containing -inf. Returns -inf if no finite values exist.
    """
    finite_mask = np.isfinite(log_values)
    if not finite_mask.any():
        return -np.inf
    finite_vals = log_values[finite_mask]
    max_val = float(finite_vals.max())
    return float(max_val + np.log(np.sum(np.exp(finite_vals - max_val))))


def _weighted_quantile(
    values: np.ndarray,
    weights: np.ndarray,
    q: float,
) -> float:
    """
    Weighted quantile via sorted cumulative weight.

    Parameters
    ----------
    values : 1D array of scalar values
    weights : non-negative weights (need not sum to 1)
    q : quantile in [0, 1]

    Returns
    -------
    float
        The q-th weighted quantile.
    """
    if len(values) == 0 or weights.sum() <= 0:
        return float("nan")
    sorted_idx = np.argsort(values)
    sv = values[sorted_idx]
    sw = weights[sorted_idx]
    csw = np.cumsum(sw)
    csw /= csw[-1]  # normalize to [0, 1]
    idx = int(np.searchsorted(csw, q))
    idx = min(idx, len(sv) - 1)
    return float(sv[idx])


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def compute_posterior(
    simulation_results: List[CandidateSimulationResult],
    proposal_densities: List[Optional[float]],
    detection_time: datetime,
    config: LikelihoodConfig = LikelihoodConfig(),
) -> BayesianInferenceResult:
    """
    Compute approximate posterior over candidate source hypotheses.

    Parameters
    ----------
    simulation_results : List[CandidateSimulationResult]
        Output of simulate_candidates() from Step 5.
    proposal_densities : List[Optional[float]]
        KDE proposal density q̃(θᵢ) for each candidate, in the same order.
        From CandidateSourceHypothesis.proposal_density (Step 4).
        None or ≤ 0 → zero importance weight for that candidate.
    detection_time : datetime
        The observation time (UTC-aware).
    config : LikelihoodConfig
        Likelihood uncertainty parameters (default: uncalibrated v1 values).

    Returns
    -------
    BayesianInferenceResult
        Approximate posterior as weighted particle set.

    Raises
    ------
    ValueError
        If simulation_results is empty, or lengths mismatch, or config invalid.
    """
    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    if len(simulation_results) == 0:
        raise ValueError("simulation_results must not be empty")
    if len(proposal_densities) != len(simulation_results):
        raise ValueError(
            f"proposal_densities length ({len(proposal_densities)}) must match "
            f"simulation_results length ({len(simulation_results)})"
        )
    # LikelihoodConfig validates its own sigmas in __post_init__

    N = len(simulation_results)
    warnings: List[str] = []

    # ------------------------------------------------------------------
    # Collect arrays
    # ------------------------------------------------------------------
    candidate_ids = [r.candidate_id for r in simulation_results]
    source_lats   = [r.source_lat   for r in simulation_results]
    source_lons   = [r.source_lon   for r in simulation_results]
    release_times = [r.release_time for r in simulation_results]

    # ------------------------------------------------------------------
    # Compute log quantities per candidate
    # ------------------------------------------------------------------
    log_lls  = np.full(N, -np.inf)
    log_pris = np.zeros(N)       # uniform prior: log p = 0
    log_pros = np.full(N, -np.inf)
    log_wu   = np.full(N, -np.inf)

    n_evaluated = 0

    for i, (r, q) in enumerate(zip(simulation_results, proposal_densities)):
        # Log-likelihood
        if r.success and r.comparison is not None:
            ll = log_likelihood(r.comparison, config)
            log_lls[i] = ll if math.isfinite(ll) else -np.inf
            n_evaluated += 1
        # else remains -inf

        # Log-proposal
        if q is not None and q > 0.0 and math.isfinite(q):
            log_pros[i] = math.log(q)
        # else remains -inf (zero-weight)

        # Log-unnormalized weight = ll + lp - lq
        lp = log_pris[i]
        if math.isfinite(log_lls[i]) and math.isfinite(log_pros[i]):
            log_wu[i] = log_lls[i] + lp - log_pros[i]
        # else remains -inf

    # ------------------------------------------------------------------
    # Normalize (log-sum-exp)
    # ------------------------------------------------------------------
    log_Z = _logsumexp(log_wu)
    if not math.isfinite(log_Z):
        # All candidates have zero weight — apply equal-weight fallback
        warnings.append(
            "All candidates have zero importance weight (all failed or "
            "proposal_density=0). Applying equal-weight fallback (w=1/N)."
        )
        norm_weights = np.full(N, 1.0 / N)
        log_wu_norm = np.full(N, math.log(1.0 / N))
    else:
        log_wu_norm = log_wu - log_Z
        norm_weights = np.exp(np.where(np.isfinite(log_wu_norm), log_wu_norm, -np.inf))
        norm_weights = np.where(np.isfinite(norm_weights), norm_weights, 0.0)
        # Re-normalize to correct floating-point drift
        total = norm_weights.sum()
        if total > 0:
            norm_weights /= total

    # ------------------------------------------------------------------
    # ESS
    # ------------------------------------------------------------------
    ess = float(1.0 / np.sum(norm_weights ** 2)) if np.sum(norm_weights ** 2) > 0 else 0.0
    ess_ratio = ess / N

    # ------------------------------------------------------------------
    # MAP candidate
    # ------------------------------------------------------------------
    map_idx = int(np.argmax(norm_weights))
    map_id  = candidate_ids[map_idx]
    map_w   = float(norm_weights[map_idx])

    # ------------------------------------------------------------------
    # Zero-weight count
    # ------------------------------------------------------------------
    n_zero_weight = int(np.sum(norm_weights == 0.0))

    # ------------------------------------------------------------------
    # Spatial posterior summary
    # ------------------------------------------------------------------
    lats = np.array(source_lats)
    lons = np.array(source_lons)
    w    = norm_weights

    weighted_mean_lat = float(np.sum(w * lats))
    weighted_mean_lon = float(np.sum(w * lons))

    m_lat = 111_319.0
    m_lon = m_lat * math.cos(math.radians(weighted_mean_lat)) + 1e-10

    weighted_std_lat_m = float(
        math.sqrt(max(0.0, np.sum(w * (lats - weighted_mean_lat) ** 2))) * m_lat
    )
    weighted_std_lon_m = float(
        math.sqrt(max(0.0, np.sum(w * (lons - weighted_mean_lon) ** 2))) * m_lon
    )

    spatial_posterior_summary = {
        "weighted_mean_lat":  weighted_mean_lat,
        "weighted_mean_lon":  weighted_mean_lon,
        "weighted_std_lat_m": weighted_std_lat_m,
        "weighted_std_lon_m": weighted_std_lon_m,
        "map_lat":            source_lats[map_idx],
        "map_lon":            source_lons[map_idx],
    }

    # ------------------------------------------------------------------
    # Temporal posterior summary
    # ------------------------------------------------------------------
    # Convert release_times to elapsed_seconds_back from detection_time
    elapsed_s = np.array([
        (detection_time - rt).total_seconds() for rt in release_times
    ])

    mean_elapsed_s  = float(np.sum(w * elapsed_s))
    median_elapsed_s = _weighted_quantile(elapsed_s, w, 0.5)
    q25_elapsed_s    = _weighted_quantile(elapsed_s, w, 0.25)
    q75_elapsed_s    = _weighted_quantile(elapsed_s, w, 0.75)
    q05_elapsed_s    = _weighted_quantile(elapsed_s, w, 0.05)
    q95_elapsed_s    = _weighted_quantile(elapsed_s, w, 0.95)

    def _elapsed_to_dt(s: float) -> datetime:
        return detection_time - timedelta(seconds=float(s))

    # Release-time distribution: (release_time, weight) per unique release
    rt_dist: Dict[datetime, float] = {}
    for rt, wi in zip(release_times, norm_weights):
        rt_dist[rt] = rt_dist.get(rt, 0.0) + float(wi)
    rt_dist_list = sorted(rt_dist.items(), key=lambda x: x[0])

    temporal_posterior_summary = {
        "weighted_mean_release_time":   _elapsed_to_dt(mean_elapsed_s),
        "weighted_median_release_time": _elapsed_to_dt(median_elapsed_s),
        "credible_50_lo":               _elapsed_to_dt(q75_elapsed_s),   # earlier = larger elapsed
        "credible_50_hi":               _elapsed_to_dt(q25_elapsed_s),
        "credible_90_lo":               _elapsed_to_dt(q95_elapsed_s),
        "credible_90_hi":               _elapsed_to_dt(q05_elapsed_s),
        "release_time_distribution":    rt_dist_list,
    }

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    inference_metadata = {
        "method":               "importance_sampling",
        "likelihood_model":     "2d_gaussian_centroid_lognormal_area",
        "prior_type":           "uniform_degree_space",
        "proposal_type":        "kde_3d_scaled",
        "jacobian_correction":  "none_required_cancels_in_normalization",
        "sigma_centroid_m":     config.sigma_centroid_m,
        "sigma_area_log":       config.sigma_area_log,
        "use_iou":              config.use_iou,
        "n_total":              N,
        "n_evaluated":          n_evaluated,
        "n_zero_weight":        n_zero_weight,
        "ess":                  ess,
        "ess_ratio":            ess_ratio,
        "warnings":             warnings,
    }

    return BayesianInferenceResult(
        candidate_ids=candidate_ids,
        source_lats=source_lats,
        source_lons=source_lons,
        release_times=release_times,
        log_likelihoods=list(log_lls),
        log_priors=list(log_pris),
        log_proposals=list(log_pros),
        log_weights_unnorm=list(log_wu),
        normalized_weights=list(norm_weights),
        effective_sample_size=ess,
        ess_ratio=ess_ratio,
        map_candidate_id=map_id,
        map_candidate_weight=map_w,
        n_total=N,
        n_evaluated=n_evaluated,
        n_zero_weight=n_zero_weight,
        spatial_posterior_summary=spatial_posterior_summary,
        temporal_posterior_summary=temporal_posterior_summary,
        inference_metadata=inference_metadata,
    )
