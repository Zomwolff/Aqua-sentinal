"""
Step 9: Origin Probability + Uncertainty — origin_probability.py

PURPOSE
=======
This is the final inference/output layer for the Aqua-Sentinel backward
hindcasting pipeline. It transforms the discrete posterior over candidate
source hypotheses (from Bayesian importance sampling, Step 6/8) into
interpretable source-origin probability and uncertainty outputs.

PIPELINE POSITION
=================
observed spill
    ↓ historical forcing
    ↓ backward advection ensemble         (Step 3)
    ↓ candidate source hypotheses          (Step 4/5)
    ↓ forward candidate simulations        (Step 6)
    ↓ footprint comparison                 (Step 7)
    ↓ observation likelihood               (Step 6)
    ↓ Bayesian importance sampling         (Step 8)
    ↓ [THIS MODULE]
    ↓ origin probability + uncertainty     (Step 9)

WHAT THIS MODULE ADDS
=====================
The existing BayesianInferenceResult (bayesian_inference.py) already provides:
- normalized_weights, candidate locations and times
- MAP candidate, ESS, weighted spatial/temporal summaries
- Credible interval endpoints for release time

This module adds:
1. Clean OriginProbabilityResult dataclass with full posterior schema
2. Spatial credible regions (50%, 90%) as discrete candidate subsets
3. Temporal credible intervals (already in bayesian_inference; surfaced here)
4. Joint source/time posterior table
5. Posterior quality flags (ESS, concentration, coverage diagnostics)
6. Candidate-set diagnostics
7. Visualization-ready structures
8. Synthetic validation integration (truth metrics — production separate)

STATISTICAL TERMINOLOGY
=======================
This module uses Bayesian terminology:
- "posterior probability" — the normalized importance weight P(θ_i | y)
- "posterior credible region" — the region containing a specified fraction
  of posterior probability mass. NOT a "confidence interval."
- "posterior concentration" — high weight on few candidates (low ESS)
- A high posterior weight does NOT mean confirmed physical truth.

SCIENTIFIC LIMITATIONS
======================
1. Posterior is conditional on current Oil Spread V2 forward physics.
2. Posterior is conditional on single-point historical forcing.
3. Backward proposal does not exactly reverse diffusion/Fay/weathering.
4. Likelihood parameters (sigma_centroid, sigma_area) are not yet calibrated.
5. Synthetic validation is model-consistent — not independent real-world.
6. High posterior probability ≠ confirmed physical source location.
7. Diffuse posterior = current data does not strongly identify source.
8. Low ESS = importance-sampling concentration; interpret as diagnostic.
9. Candidate set size limits posterior resolution.

WHAT THIS MODULE DOES NOT DO
=============================
- Does NOT modify simulate_ensemble() or any Oil Spread V2 physics
- Does NOT change sigma_centroid_m, sigma_area_log, or any defaults
- Does NOT implement AIS, vessel attribution, ML, or severity analysis
- Does NOT reimplement Bayesian importance sampling
- Does NOT claim real-world validation
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.bayesian_inference import BayesianInferenceResult
from app.footprint_comparison import _haversine


# ─────────────────────────────────────────────────────────────────
# Quality flag constants
# ─────────────────────────────────────────────────────────────────

FLAG_LOW_ESS                   = "LOW_ESS"
FLAG_HIGH_WEIGHT_CONCENTRATION = "HIGH_WEIGHT_CONCENTRATION"
FLAG_DIFFUSE_SPATIAL_POSTERIOR = "DIFFUSE_SPATIAL_POSTERIOR"
FLAG_DIFFUSE_TEMPORAL_POSTERIOR = "DIFFUSE_TEMPORAL_POSTERIOR"
FLAG_LIMITED_CANDIDATE_COVERAGE = "LIMITED_CANDIDATE_COVERAGE"
FLAG_STRONG_POSTERIOR_CONCENTRATION = "STRONG_POSTERIOR_CONCENTRATION"
FLAG_EQUAL_WEIGHT_FALLBACK     = "EQUAL_WEIGHT_FALLBACK"
FLAG_ALL_ZERO_WEIGHTS          = "ALL_ZERO_WEIGHTS"


# ─────────────────────────────────────────────────────────────────
# Posterior candidate entry (for joint source-time table)
# ─────────────────────────────────────────────────────────────────

@dataclass
class PosteriorCandidate:
    """
    One candidate source hypothesis with its posterior weight.
    This is the joint (lat, lon, release_time) posterior representation.
    """
    candidate_id:    int
    source_lat:      float
    source_lon:      float
    release_time:    datetime    # UTC-aware
    posterior_weight: float      # normalized; sums to 1 across all candidates


# ─────────────────────────────────────────────────────────────────
# Spatial credible region
# ─────────────────────────────────────────────────────────────────

@dataclass
class SpatialCredibleRegion:
    """
    A discrete posterior credible region: the minimum set of candidates
    whose cumulative posterior weight exceeds the target level.

    This is a highest-posterior-density (HPD) subset, not a geometric circle.
    Do NOT interpret this as a circle or ellipse — it is a discrete weighted
    candidate cloud.

    For visualisation, the convex hull of the candidate locations can be
    computed separately; it is stored in hull_wkt if provided.
    """
    level:              float          # 0.50 or 0.90
    candidates:         List[PosteriorCandidate]
    cumulative_weight:  float          # actual weight in this region (≥ level)
    n_candidates:       int
    spatial_extent_km:  float          # max haversine distance within region (km)
    hull_wkt:           Optional[str] = None   # WKT polygon of convex hull (if computed)


# ─────────────────────────────────────────────────────────────────
# Temporal posterior summary
# ─────────────────────────────────────────────────────────────────

@dataclass
class TemporalPosterior:
    """
    Posterior distribution over release time, derived from importance weights.
    All intervals are Bayesian CREDIBLE INTERVALS — not confidence intervals.
    """
    map_release_time:          datetime       # release time of MAP candidate
    weighted_mean_release_time: datetime      # Σ wᵢ * release_time_i
    credible_50_lo:            datetime       # 25th weighted quantile
    credible_50_hi:            datetime       # 75th weighted quantile
    credible_90_lo:            datetime       # 5th weighted quantile
    credible_90_hi:            datetime       # 95th weighted quantile
    spread_hours:              float          # 90% interval width in hours
    distribution:              List[Tuple[datetime, float]]  # (time, weight)


# ─────────────────────────────────────────────────────────────────
# Spatial posterior summary
# ─────────────────────────────────────────────────────────────────

@dataclass
class SpatialPosterior:
    """
    Posterior distribution over source location, derived from importance weights.
    Coordinates are WGS-84 decimal degrees.
    """
    map_lat:               float
    map_lon:               float
    weighted_centroid_lat: float   # Σ wᵢ * latᵢ
    weighted_centroid_lon: float   # Σ wᵢ * lonᵢ
    weighted_std_lat_m:    float   # weighted std in metres (N-S)
    weighted_std_lon_m:    float   # weighted std in metres (E-W)
    weighted_rms_spread_m: float   # sqrt(Σ wᵢ * dist(i, centroid)²)
    credible_50:           SpatialCredibleRegion
    credible_90:           SpatialCredibleRegion


# ─────────────────────────────────────────────────────────────────
# Posterior diagnostics
# ─────────────────────────────────────────────────────────────────

@dataclass
class PosteriorDiagnostics:
    """
    Statistical diagnostics for the importance-sampling posterior.
    All values are diagnostics — they do not establish physical truth.
    """
    ess:                         float   # effective sample size = 1 / Σ wᵢ²
    ess_ratio:                   float   # ESS / N_total
    max_posterior_weight:        float   # max(wᵢ)
    n_total_candidates:          int
    n_nonzero_weight_candidates: int     # candidates with wᵢ > 0
    n_significant_candidates:    int     # candidates with wᵢ ≥ 0.01
    n_unique_release_times:      int
    candidate_spatial_extent_km: float   # max pairwise haversine distance (km)
    candidate_time_extent_h:     float   # max - min release time in hours
    quality_flags:               List[str]
    warnings:                    List[str]


# ─────────────────────────────────────────────────────────────────
# Main result
# ─────────────────────────────────────────────────────────────────

@dataclass
class OriginProbabilityResult:
    """
    Step 9 final output: source-origin probability and uncertainty.

    SCIENTIFIC DISCLAIMER:
    This posterior is conditional on the current forward model, forcing,
    and likelihood parameters. A high posterior probability does NOT mean
    confirmed physical truth. A diffuse posterior means the available
    information does not strongly identify the source. See module docstring
    for full scientific limitations.

    TERMINOLOGY:
    - posterior_weight: P(θᵢ | y) normalized importance weight
    - credible region: Bayesian credible region (NOT a confidence interval)
    - MAP: Maximum A-Posteriori candidate (highest posterior weight)
    """
    inference_status:    str             # "success" | "fallback_equal_weights" | "failed"

    # Joint source-time posterior (all candidates)
    posterior_candidates: List[PosteriorCandidate]

    # Spatial posterior
    spatial_posterior:    SpatialPosterior

    # Temporal posterior
    temporal_posterior:   TemporalPosterior

    # Diagnostics
    diagnostics:          PosteriorDiagnostics

    # Metadata
    detection_time:       datetime
    n_forcing_samples:    Optional[int] = None
    generation_method:    str = "importance_sampling_kde_3d_scaled"
    provenance:           Dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _weighted_quantile_elapsed(
    release_times: List[datetime],
    weights: np.ndarray,
    detection_time: datetime,
    q: float,
) -> datetime:
    """Weighted quantile over elapsed seconds from detection_time."""
    elapsed = np.array([(detection_time - rt).total_seconds() for rt in release_times])
    sorted_idx = np.argsort(elapsed)
    se = elapsed[sorted_idx]
    sw = weights[sorted_idx]
    csw = np.cumsum(sw)
    if csw[-1] > 0:
        csw /= csw[-1]
    idx = min(int(np.searchsorted(csw, q)), len(se) - 1)
    return detection_time - timedelta(seconds=float(se[idx]))


def _build_spatial_credible_region(
    level: float,
    candidates: List[PosteriorCandidate],
    centroid_lat: float,
    centroid_lon: float,
) -> SpatialCredibleRegion:
    """
    Build an HPD credible region: sort candidates by posterior weight descending,
    accumulate until cumulative weight ≥ level.
    """
    sorted_cands = sorted(candidates, key=lambda c: c.posterior_weight, reverse=True)
    region = []
    cumulative = 0.0
    for c in sorted_cands:
        region.append(c)
        cumulative += c.posterior_weight
        if cumulative >= level:
            break

    # Spatial extent: max haversine distance between any two candidates in region
    max_dist_km = 0.0
    for i in range(len(region)):
        for j in range(i + 1, len(region)):
            d = _haversine(region[i].source_lat, region[i].source_lon,
                           region[j].source_lat, region[j].source_lon)
            max_dist_km = max(max_dist_km, d / 1000.0)

    return SpatialCredibleRegion(
        level=level,
        candidates=region,
        cumulative_weight=float(cumulative),
        n_candidates=len(region),
        spatial_extent_km=float(max_dist_km),
    )


def _compute_quality_flags(
    w: np.ndarray,
    ess_ratio: float,
    spatial_rms_m: float,
    spread_hours: float,
    candidate_spatial_extent_km: float,
    candidate_time_extent_h: float,
    inference_status: str,
) -> List[str]:
    """Derive posterior quality flags from diagnostics."""
    flags = []

    if inference_status == "fallback_equal_weights":
        flags.append(FLAG_EQUAL_WEIGHT_FALLBACK)

    if inference_status == "failed":
        flags.append(FLAG_ALL_ZERO_WEIGHTS)
        return flags

    if ess_ratio < 0.1:
        flags.append(FLAG_LOW_ESS)

    if w.max() > 0.5:
        flags.append(FLAG_HIGH_WEIGHT_CONCENTRATION)
    elif w.max() < 0.1 and ess_ratio > 0.5:
        flags.append(FLAG_STRONG_POSTERIOR_CONCENTRATION if ess_ratio < 0.2
                     else FLAG_DIFFUSE_SPATIAL_POSTERIOR)

    if spatial_rms_m > 20_000:
        flags.append(FLAG_DIFFUSE_SPATIAL_POSTERIOR)

    if spread_hours > 12.0:
        flags.append(FLAG_DIFFUSE_TEMPORAL_POSTERIOR)

    if candidate_spatial_extent_km < 5.0:
        flags.append(FLAG_LIMITED_CANDIDATE_COVERAGE)

    if ess_ratio >= 0.3 and w.max() < 0.2:
        if FLAG_STRONG_POSTERIOR_CONCENTRATION not in flags:
            flags.append(FLAG_STRONG_POSTERIOR_CONCENTRATION
                         if False else FLAG_DIFFUSE_SPATIAL_POSTERIOR
                         if FLAG_DIFFUSE_SPATIAL_POSTERIOR not in flags
                         else None)

    return [f for f in flags if f is not None]


# ─────────────────────────────────────────────────────────────────
# Main public function
# ─────────────────────────────────────────────────────────────────

def compute_origin_probability(
    posterior: BayesianInferenceResult,
    detection_time: Optional[datetime] = None,
    significance_threshold: float = 0.01,
    spatial_rms_diffuse_threshold_m: float = 20_000.0,
    temporal_spread_diffuse_threshold_h: float = 12.0,
    n_forcing_samples: Optional[int] = None,
) -> OriginProbabilityResult:
    """
    Transform the Bayesian importance-sampling posterior into a clean
    source-origin probability + uncertainty result (Step 9).

    Parameters
    ----------
    posterior : BayesianInferenceResult
        Output of bayesian_inference.compute_posterior().
    detection_time : datetime, optional
        UTC-aware detection time. Inferred from posterior.detection_time
        if not provided.
    significance_threshold : float
        Minimum posterior weight for a candidate to be counted "significant".
    spatial_rms_diffuse_threshold_m : float
        Spatial RMS spread above which FLAG_DIFFUSE_SPATIAL_POSTERIOR is set.
    temporal_spread_diffuse_threshold_h : float
        90% credible interval width above which FLAG_DIFFUSE_TEMPORAL_POSTERIOR.
    n_forcing_samples : int, optional
        Number of forcing samples used (for provenance).

    Returns
    -------
    OriginProbabilityResult
        Full posterior representation with spatial/temporal distributions,
        credible regions, and diagnostic flags.
    """
    if detection_time is None:
        detection_time = posterior.detection_time

    N = posterior.n_total
    w = np.array(posterior.normalized_weights)
    lats = np.array(posterior.source_lats)
    lons = np.array(posterior.source_lons)
    rts  = posterior.release_times

    # ── Determine inference status ──────────────────────────────
    warnings_list = list(posterior.inference_metadata.get("warnings", []))
    if FLAG_EQUAL_WEIGHT_FALLBACK in " ".join(warnings_list) or \
       "equal-weight" in " ".join(warnings_list).lower():
        status = "fallback_equal_weights"
    elif posterior.n_evaluated == 0:
        status = "failed"
    else:
        status = "success"

    # ── Build joint posterior candidates ────────────────────────
    posterior_candidates = [
        PosteriorCandidate(
            candidate_id=posterior.candidate_ids[i],
            source_lat=float(lats[i]),
            source_lon=float(lons[i]),
            release_time=rts[i],
            posterior_weight=float(w[i]),
        )
        for i in range(N)
    ]

    # ── Spatial posterior ────────────────────────────────────────
    sps = posterior.spatial_posterior_summary
    c_lat = float(sps["weighted_mean_lat"])
    c_lon = float(sps["weighted_mean_lon"])
    m_lat = 111_319.0
    m_lon = m_lat * math.cos(math.radians(c_lat)) + 1e-10

    # Weighted RMS from weighted centroid
    dists = np.array([_haversine(c_lat, c_lon, la, lo) for la, lo in zip(lats, lons)])
    weighted_rms = float(np.sqrt(np.sum(w * dists**2)))

    cr50 = _build_spatial_credible_region(0.50, posterior_candidates, c_lat, c_lon)
    cr90 = _build_spatial_credible_region(0.90, posterior_candidates, c_lat, c_lon)

    spatial_posterior = SpatialPosterior(
        map_lat=float(sps["map_lat"]),
        map_lon=float(sps["map_lon"]),
        weighted_centroid_lat=c_lat,
        weighted_centroid_lon=c_lon,
        weighted_std_lat_m=float(sps.get("weighted_std_lat_m", 0.0)),
        weighted_std_lon_m=float(sps.get("weighted_std_lon_m", 0.0)),
        weighted_rms_spread_m=weighted_rms,
        credible_50=cr50,
        credible_90=cr90,
    )

    # ── Temporal posterior ───────────────────────────────────────
    ts  = posterior.temporal_posterior_summary
    map_idx = int(np.argmax(w))
    map_rt  = rts[map_idx]

    cr50_lo = ts["credible_50_lo"]
    cr50_hi = ts["credible_50_hi"]
    cr90_lo = ts["credible_90_lo"]
    cr90_hi = ts["credible_90_hi"]

    spread_h = abs((max(cr90_lo, cr90_hi) - min(cr90_lo, cr90_hi)).total_seconds()) / 3600.0

    # Build distribution (time, weight) aggregated per unique release time
    rt_dist: Dict[datetime, float] = {}
    for rt, wi in zip(rts, w):
        rt_dist[rt] = rt_dist.get(rt, 0.0) + float(wi)
    rt_dist_list = sorted(rt_dist.items(), key=lambda x: x[0])

    temporal_posterior = TemporalPosterior(
        map_release_time=map_rt,
        weighted_mean_release_time=ts["weighted_mean_release_time"],
        credible_50_lo=min(cr50_lo, cr50_hi),
        credible_50_hi=max(cr50_lo, cr50_hi),
        credible_90_lo=min(cr90_lo, cr90_hi),
        credible_90_hi=max(cr90_lo, cr90_hi),
        spread_hours=spread_h,
        distribution=rt_dist_list,
    )

    # ── Diagnostics ─────────────────────────────────────────────
    n_nonzero = int(np.sum(w > 0))
    n_sig     = int(np.sum(w >= significance_threshold))

    # Candidate spatial extent (bounding box diagonal approx)
    if N > 1:
        cand_extent_km = float(_haversine(lats.min(), lons.min(),
                                          lats.max(), lons.max()) / 1000.0)
    else:
        cand_extent_km = 0.0

    elapsed_s = np.array([(detection_time - rt).total_seconds() for rt in rts])
    cand_time_h = float((elapsed_s.max() - elapsed_s.min()) / 3600.0) if N > 1 else 0.0

    unique_rts = len(set(rts))

    flags = _compute_quality_flags(
        w=w,
        ess_ratio=posterior.ess_ratio,
        spatial_rms_m=weighted_rms,
        spread_hours=spread_h,
        candidate_spatial_extent_km=cand_extent_km,
        candidate_time_extent_h=cand_time_h,
        inference_status=status,
    )

    diagnostics = PosteriorDiagnostics(
        ess=posterior.effective_sample_size,
        ess_ratio=posterior.ess_ratio,
        max_posterior_weight=float(w.max()),
        n_total_candidates=N,
        n_nonzero_weight_candidates=n_nonzero,
        n_significant_candidates=n_sig,
        n_unique_release_times=unique_rts,
        candidate_spatial_extent_km=cand_extent_km,
        candidate_time_extent_h=cand_time_h,
        quality_flags=flags,
        warnings=warnings_list,
    )

    # ── Provenance ───────────────────────────────────────────────
    provenance = {
        "bayesian_method":     "importance_sampling",
        "likelihood_model":    posterior.inference_metadata.get("likelihood_model", "unknown"),
        "prior_type":          posterior.inference_metadata.get("prior_type", "unknown"),
        "proposal_type":       posterior.inference_metadata.get("proposal_type", "unknown"),
        "sigma_centroid_m":    posterior.inference_metadata.get("sigma_centroid_m"),
        "sigma_area_log":      posterior.inference_metadata.get("sigma_area_log"),
        "n_forcing_samples":   n_forcing_samples,
        "scientific_disclaimer": (
            "Posterior conditional on forward physics, forcing, and likelihood parameters. "
            "High posterior weight ≠ confirmed physical truth. "
            "Diffuse posterior = insufficient information to identify source."
        ),
    }

    return OriginProbabilityResult(
        inference_status=status,
        posterior_candidates=posterior_candidates,
        spatial_posterior=spatial_posterior,
        temporal_posterior=temporal_posterior,
        diagnostics=diagnostics,
        detection_time=detection_time,
        n_forcing_samples=n_forcing_samples,
        generation_method="importance_sampling_kde_3d_scaled",
        provenance=provenance,
    )


# ─────────────────────────────────────────────────────────────────
# Synthetic validation helper (ONLY for known-truth experiments)
# ─────────────────────────────────────────────────────────────────

@dataclass
class OriginValidationMetrics:
    """
    Metrics comparing posterior against known truth.
    ONLY for synthetic validation — NEVER used in production inference.
    The true_source_* fields must NOT appear in OriginProbabilityResult.
    """
    true_source_lat:             float
    true_source_lon:             float
    true_release_time:           datetime
    map_spatial_error_m:         float
    map_time_error_s:            float
    true_in_spatial_50:          bool
    true_in_spatial_90:          bool
    true_in_temporal_50:         bool
    true_in_temporal_90:         bool
    posterior_mass_near_truth:   float   # weight within spatial+temporal thresholds
    spatial_threshold_m:         float
    temporal_threshold_s:        float


def evaluate_origin_against_truth(
    result: OriginProbabilityResult,
    true_source_lat: float,
    true_source_lon: float,
    true_release_time: datetime,
    spatial_threshold_m: float = 20_000.0,
    temporal_threshold_s: float = 3_600.0,
) -> OriginValidationMetrics:
    """
    Compare OriginProbabilityResult against known truth.
    USE ONLY in synthetic validation — not in production inference.
    """
    sp = result.spatial_posterior
    tp = result.temporal_posterior

    # MAP errors
    map_spatial_err = _haversine(true_source_lat, true_source_lon,
                                  sp.map_lat, sp.map_lon)
    map_time_err = abs((tp.map_release_time - true_release_time).total_seconds())

    # Credible region containment (discrete: is truth near any candidate in region?)
    def _in_region(region: SpatialCredibleRegion) -> bool:
        for c in region.candidates:
            if _haversine(true_source_lat, true_source_lon,
                          c.source_lat, c.source_lon) <= spatial_threshold_m:
                return True
        return False

    in_s50 = _in_region(sp.credible_50)
    in_s90 = _in_region(sp.credible_90)

    # Temporal credible interval containment
    in_t50 = tp.credible_50_lo <= true_release_time <= tp.credible_50_hi
    in_t90 = tp.credible_90_lo <= true_release_time <= tp.credible_90_hi

    # Posterior mass near truth
    mass = sum(
        c.posterior_weight
        for c in result.posterior_candidates
        if _haversine(true_source_lat, true_source_lon,
                      c.source_lat, c.source_lon) <= spatial_threshold_m
        and abs((c.release_time - true_release_time).total_seconds()) <= temporal_threshold_s
    )

    return OriginValidationMetrics(
        true_source_lat=true_source_lat,
        true_source_lon=true_source_lon,
        true_release_time=true_release_time,
        map_spatial_error_m=map_spatial_err,
        map_time_error_s=map_time_err,
        true_in_spatial_50=in_s50,
        true_in_spatial_90=in_s90,
        true_in_temporal_50=in_t50,
        true_in_temporal_90=in_t90,
        posterior_mass_near_truth=float(mass),
        spatial_threshold_m=spatial_threshold_m,
        temporal_threshold_s=temporal_threshold_s,
    )


# ─────────────────────────────────────────────────────────────────
# Pretty-print helpers
# ─────────────────────────────────────────────────────────────────

def print_origin_result(result: OriginProbabilityResult) -> None:
    """Print a human-readable posterior summary."""
    sp = result.spatial_posterior
    tp = result.temporal_posterior
    d  = result.diagnostics

    print("\n" + "="*70)
    print("ORIGIN PROBABILITY RESULT — Step 9")
    print("="*70)
    print(f"  Inference status:     {result.inference_status}")
    print(f"  Detection time:       {result.detection_time.isoformat()}")

    print("\n  MAP SOURCE:")
    print(f"    lat={sp.map_lat:.5f}°  lon={sp.map_lon:.5f}°")
    print(f"    release_time: {tp.map_release_time.isoformat()}")
    print(f"    posterior_weight: {d.max_posterior_weight:.4f}")

    print("\n  SPATIAL POSTERIOR:")
    print(f"    weighted centroid:  ({sp.weighted_centroid_lat:.5f}°, {sp.weighted_centroid_lon:.5f}°)")
    print(f"    weighted RMS spread: {sp.weighted_rms_spread_m/1000:.2f} km")
    print(f"    50% credible region: {sp.credible_50.n_candidates} candidates, "
          f"extent={sp.credible_50.spatial_extent_km:.1f} km")
    print(f"    90% credible region: {sp.credible_90.n_candidates} candidates, "
          f"extent={sp.credible_90.spatial_extent_km:.1f} km")

    print("\n  TEMPORAL POSTERIOR:")
    print(f"    MAP release time:    {tp.map_release_time.isoformat()}")
    print(f"    Weighted mean:       {tp.weighted_mean_release_time.isoformat()}")
    print(f"    50% credible:        [{tp.credible_50_lo.isoformat()}, {tp.credible_50_hi.isoformat()}]")
    print(f"    90% credible:        [{tp.credible_90_lo.isoformat()}, {tp.credible_90_hi.isoformat()}]")
    print(f"    90% spread:          {tp.spread_hours:.2f} hours")

    print("\n  DIAGNOSTICS:")
    print(f"    ESS={d.ess:.1f}  ESS%={d.ess_ratio*100:.1f}  "
          f"max_w={d.max_posterior_weight:.4f}  N={d.n_total_candidates}")
    print(f"    significant candidates: {d.n_significant_candidates}")
    print(f"    quality flags: {d.quality_flags if d.quality_flags else 'none'}")
    print("="*70)
