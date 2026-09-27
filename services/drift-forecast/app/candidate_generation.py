"""
Step 4: Candidate Source Hypothesis Generation — candidate_generation.py

WHAT THIS MODULE DOES:
    Converts the backward trajectory cloud from backward_hindcast.backward_propagate()
    into a manageable set of discrete candidate source hypotheses, each representing:
        θ = (source_lat, source_lon, release_time)

    These candidates are the search space for the next stage, where each will be
    evaluated by running the existing forward Oil Spread V2 model (simulate_ensemble)
    and comparing the resulting footprint against the observed spill.

ALGORITHM:
    1. Flatten trajectory_history into raw states (lat, lon, elapsed_hours_back).
    2. Scale coordinates for numerical comparability across dimensions.
    3. Fit a 3D Gaussian KDE over scaled states using scipy.stats.gaussian_kde.
    4. Resample candidate points from the KDE.
    5. Filter out-of-bounds samples (with 0.5° margin for KDE smoothing).
    6. Snap release_time to the nearest 600 s grid.
    7. Deduplicate near-identical candidates.
    8. Compute backward_support per candidate (fraction of particles within 5 km).

SCIENTIFIC DISCLAIMER:
    Candidates are HYPOTHESES derived from backward advection transport, NOT
    confirmed origins or posterior probabilities. The backward ensemble excludes
    diffusion, Fay spreading, and weathering. Candidate quality depends entirely
    on the quality of the backward ensemble and the forcing data. The 'proposal_density'
    stored per candidate is the KDE density q(θ) — it is not a probability and
    must not be interpreted as one.

WHAT THIS MODULE DOES NOT DO:
    - Does NOT call simulate_ensemble() or any forward physics
    - Does NOT compute IoU, likelihood, or Bayesian weights
    - Does NOT perform Bayesian inference
    - Does NOT use AIS or vessel attribution
    - Does NOT modify any existing module
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.stats import gaussian_kde

from app.backward_hindcast import BackwardEnsembleResult
from app.particles import INTEGRATION_STEP_S

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORT_RADIUS_M: float = 5_000.0   # 5 km radius for backward_support computation
OVERSAMPLE_FACTOR: int  = 5          # oversample multiplier for KDE rejection sampling
MAX_OVERSAMPLE_ROUNDS: int = 4       # maximum rounds of resampling before giving up
GENERATION_METHOD: str  = "kde_3d_scaled"
SPATIAL_MARGIN_DEG: float = 0.5     # degrees margin beyond raw state bounds


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CandidateSourceHypothesis:
    """
    A single candidate source hypothesis derived from backward advection.

    SCIENTIFIC NOTE:
    This is a hypothesis, NOT a confirmed origin. It is a sample drawn from
    the backward-transport proposal distribution. The 'proposal_density' is
    the KDE density q(θ) — it is not a posterior probability and must not
    be interpreted as one. Posterior probabilities require forward simulation
    and Bayesian inference (implemented in a later step).

    Fields:
        candidate_id:    Sequential integer identifier.
        source_lat:      WGS-84 latitude in degrees.
        source_lon:      WGS-84 longitude in degrees.
        release_time:    UTC-aware datetime, snapped to 600 s grid.
        proposal_density: KDE value q(θ). NOT a probability or posterior.
        backward_support: Fraction of backward particles within 5 km at release_time.
                         This is a diagnostic metric, not a probability.
        timestamp_index:  Index into trajectory_history closest to release_time.
        source_generation_method: Algorithm used; always "kde_3d_scaled" here.
    """
    candidate_id:             int
    source_lat:               float
    source_lon:               float
    release_time:             datetime
    proposal_density:         Optional[float]
    backward_support:         float
    timestamp_index:          int
    source_generation_method: str


@dataclass
class CandidateGenerationResult:
    """
    Output of generate_candidates().

    Contains candidate source hypotheses and generation diagnostics.
    Candidates have NOT been evaluated by forward simulation.
    They are proposals derived from backward transport support.
    """
    candidates:        List[CandidateSourceHypothesis]
    n_candidates:      int
    detection_time:    datetime
    horizon_hours:     float
    source_statistics: Dict[str, Any]
    generation_method: str


# ---------------------------------------------------------------------------
# Haversine helpers
# ---------------------------------------------------------------------------

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Scalar haversine distance in metres."""
    R = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2.0 * R * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _haversine_vec(
    lat_arr: np.ndarray,
    lon_arr: np.ndarray,
    lat_c: float,
    lon_c: float,
) -> np.ndarray:
    """Vectorized haversine distances from array of points to a single centre (metres)."""
    R = 6_371_000.0
    phi1 = math.radians(lat_c)
    phi_p = np.radians(lat_arr)
    dphi = np.radians(lat_arr - lat_c)
    dlam = np.radians(lon_arr - lon_c)
    a = np.sin(dphi / 2) ** 2 + math.cos(phi1) * np.cos(phi_p) * np.sin(dlam / 2) ** 2
    return 2.0 * R * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def _validate_inputs(
    backward_result: BackwardEnsembleResult,
    n_candidates: int,
    spatial_bandwidth: Optional[float],
    temporal_bandwidth: Optional[float],
    spatial_dedup_m: float,
    temporal_dedup_s: float,
) -> None:
    """Validate generate_candidates() inputs. Raises ValueError on any invalid condition."""
    history = backward_result.trajectory_history
    if len(history) < 2:
        raise ValueError(
            f"trajectory_history must have at least 2 steps; got {len(history)}"
        )
    n_p = backward_result.n_particles
    for i, step in enumerate(history):
        if len(step["lat"]) != n_p:
            raise ValueError(
                f"trajectory_history[{i}]['lat'] has length {len(step['lat'])}, "
                f"expected {n_p} (n_particles)"
            )
        if len(step["lon"]) != n_p:
            raise ValueError(
                f"trajectory_history[{i}]['lon'] has length {len(step['lon'])}, "
                f"expected {n_p} (n_particles)"
            )
        if step["t"].tzinfo is None:
            raise ValueError(
                f"trajectory_history[{i}]['t'] is timezone-naive; must be UTC-aware"
            )
    if n_candidates < 1 or n_candidates > 5000:
        raise ValueError(
            f"n_candidates={n_candidates} must be in [1, 5000]"
        )
    if spatial_bandwidth is not None and spatial_bandwidth <= 0:
        raise ValueError(
            f"spatial_bandwidth={spatial_bandwidth} must be positive"
        )
    if temporal_bandwidth is not None and temporal_bandwidth <= 0:
        raise ValueError(
            f"temporal_bandwidth={temporal_bandwidth} must be positive"
        )
    if spatial_dedup_m < 0:
        raise ValueError(
            f"spatial_dedup_m={spatial_dedup_m} must be non-negative"
        )
    if temporal_dedup_s < 0:
        raise ValueError(
            f"temporal_dedup_s={temporal_dedup_s} must be non-negative"
        )


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------

def _collect_raw_states(
    trajectory_history: List[Dict[str, Any]],
    detection_time: datetime,
) -> np.ndarray:
    """
    Flatten trajectory_history into a (N_steps * n_particles, 3) matrix.

    Columns: [lat_deg, lon_deg, elapsed_hours_back]
    elapsed_hours_back = (detection_time - step["t"]).total_seconds() / 3600
    Always positive; increases as we go further back in time.
    """
    rows = []
    for step in trajectory_history:
        elapsed_h = (detection_time - step["t"]).total_seconds() / 3600.0
        lats = np.asarray(step["lat"])
        lons = np.asarray(step["lon"])
        step_rows = np.column_stack([lats, lons, np.full(len(lats), elapsed_h)])
        rows.append(step_rows)
    return np.vstack(rows)  # shape (N_steps * n_particles, 3)


# ---------------------------------------------------------------------------
# KDE construction
# ---------------------------------------------------------------------------

def _build_kde(
    states: np.ndarray,
    horizon_hours: float,
    spatial_bandwidth: Optional[float],
    temporal_bandwidth: Optional[float],
) -> Tuple[gaussian_kde, np.ndarray]:
    """
    Build a 3D Gaussian KDE over scaled (lat, lon, elapsed_hours_back).

    Scaling:
        lat_scale  = 1.0           (degrees — leave as-is)
        lon_scale  = 1.0           (degrees — leave as-is)
        time_scale = horizon_hours (normalises elapsed_hours to ~[0, 1])

    With this scaling, all three dimensions are O(1) in magnitude for a
    typical regional spill event, preventing the time axis from dominating
    Scott's bandwidth rule.

    Returns:
        (kde, scale) where scale is the array [lat_scale, lon_scale, time_scale]
        used to back-transform sampled points.
    """
    lat_scale  = 1.0
    lon_scale  = 1.0
    time_scale = max(horizon_hours, 1e-6)  # guard against zero horizon

    scale = np.array([lat_scale, lon_scale, time_scale])
    scaled = states / scale[np.newaxis, :]  # shape (N, 3)

    if spatial_bandwidth is None and temporal_bandwidth is None:
        # Default: Scott's rule on scaled data
        kde = gaussian_kde(scaled.T, bw_method="scott")
    else:
        # Custom per-dimension bandwidth — rescale each axis so that
        # gaussian_kde with bw_method=1.0 applies the desired bandwidth.
        # effective_bw[d] = bw_method * std(data_d)
        # We want bw_method * std(rescaled_d) = desired_bw_in_scaled_units
        # => rescaled_d = scaled_d / desired_bw_in_scaled_units  (so std -> 1)
        std = scaled.std(axis=0) + 1e-10

        # Convert spatial bandwidth from degrees to scaled units (already 1:1)
        desired_bw_lat  = (spatial_bandwidth  / lat_scale)  if spatial_bandwidth  is not None else (gaussian_kde(scaled.T, bw_method="scott").factor * std[0])
        desired_bw_lon  = (spatial_bandwidth  / lon_scale)  if spatial_bandwidth  is not None else (gaussian_kde(scaled.T, bw_method="scott").factor * std[1])
        desired_bw_time = (temporal_bandwidth / time_scale) if temporal_bandwidth is not None else (gaussian_kde(scaled.T, bw_method="scott").factor * std[2])

        desired_bw = np.array([desired_bw_lat, desired_bw_lon, desired_bw_time])
        rescale_factor = desired_bw + 1e-10
        rescaled = scaled / rescale_factor[np.newaxis, :]
        kde = gaussian_kde(rescaled.T, bw_method=1.0)
        # Adjust scale so back-transform is correct
        scale = scale * rescale_factor

    return kde, scale


# ---------------------------------------------------------------------------
# Time snapping
# ---------------------------------------------------------------------------

def _snap_to_step(
    elapsed_hours: float,
    detection_time: datetime,
    trajectory_history: List[Dict[str, Any]],
) -> Tuple[datetime, int]:
    """
    Convert elapsed_hours_back to a release_time snapped to INTEGRATION_STEP_S.

    Clamps to the valid range [trajectory_history[-1]["t"], detection_time].
    Returns (release_time_snapped, timestamp_index).
    """
    dt_s = INTEGRATION_STEP_S
    offset_s = elapsed_hours * 3600.0
    snapped_offset_s = round(offset_s / dt_s) * dt_s

    # Clamp: cannot be in the future (offset_s must be >= 0)
    snapped_offset_s = max(snapped_offset_s, 0.0)

    # Clamp: cannot be older than the earliest trajectory step
    # trajectory_history[-1]["t"] is the oldest step (most backward)
    earliest_offset_s = (detection_time - trajectory_history[-1]["t"]).total_seconds()
    snapped_offset_s = min(snapped_offset_s, earliest_offset_s)

    release_time = detection_time - timedelta(seconds=snapped_offset_s)
    if release_time.tzinfo is None:
        release_time = release_time.replace(tzinfo=timezone.utc)

    # Find closest trajectory_history index by timestamp
    ts_index = min(
        range(len(trajectory_history)),
        key=lambda i: abs((trajectory_history[i]["t"] - release_time).total_seconds()),
    )
    return release_time, ts_index


# ---------------------------------------------------------------------------
# Backward support
# ---------------------------------------------------------------------------

def _compute_backward_support(
    source_lat: float,
    source_lon: float,
    ts_index: int,
    trajectory_history: List[Dict[str, Any]],
) -> float:
    """
    Fraction of particles at trajectory_history[ts_index] within SUPPORT_RADIUS_M
    of (source_lat, source_lon).

    This is a diagnostic metric — NOT a probability.
    """
    step = trajectory_history[ts_index]
    dists = _haversine_vec(
        np.asarray(step["lat"]),
        np.asarray(step["lon"]),
        source_lat, source_lon,
    )
    return float(np.mean(dists < SUPPORT_RADIUS_M))


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _deduplicate(
    candidates: List[CandidateSourceHypothesis],
    spatial_dedup_m: float,
    temporal_dedup_s: float,
) -> Tuple[List[CandidateSourceHypothesis], int]:
    """
    Greedy deduplication: keep the first (highest proposal_density) candidate;
    reject any subsequent candidate within spatial_dedup_m metres AND
    temporal_dedup_s seconds of a kept one.

    Input list should be sorted by proposal_density descending.
    Returns (kept, rejected_count).
    """
    kept: List[CandidateSourceHypothesis] = []
    rejected = 0
    for cand in candidates:
        is_dup = False
        for existing in kept:
            dist_m = _haversine(
                cand.source_lat, cand.source_lon,
                existing.source_lat, existing.source_lon,
            )
            dt_s = abs(
                (cand.release_time - existing.release_time).total_seconds()
            )
            if dist_m < spatial_dedup_m and dt_s < temporal_dedup_s:
                is_dup = True
                break
        if not is_dup:
            kept.append(cand)
        else:
            rejected += 1
    return kept, rejected


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def generate_candidates(
    backward_result: BackwardEnsembleResult,
    n_candidates: int = 300,
    seed: int = 42,
    spatial_bandwidth: Optional[float] = None,
    temporal_bandwidth: Optional[float] = None,
    spatial_dedup_m: float = 1_000.0,
    temporal_dedup_s: float = 600.0,
) -> CandidateGenerationResult:
    """
    Generate candidate source hypotheses from a backward advection ensemble.

    Converts the continuous backward trajectory cloud into approximately
    n_candidates discrete (source_lat, source_lon, release_time) hypotheses
    by fitting a 3D Gaussian KDE over the backward particle states and
    sampling from it.

    SCIENTIFIC NOTE:
    Returned candidates are HYPOTHESES, not confirmed origins or probabilities.
    They are proposals from the backward-transport support distribution.
    The 'proposal_density' field stores the KDE value q(θ) for future use in
    importance sampling — it is NOT a posterior probability.

    Parameters
    ----------
    backward_result : BackwardEnsembleResult
        Output of backward_hindcast.backward_propagate(). Must have >= 2 steps.
    n_candidates : int
        Target number of candidates (default 300; valid 1–5000).
    seed : int
        RNG seed. Same seed + same input → identical output.
    spatial_bandwidth : float, optional
        KDE bandwidth in degrees for lat/lon dimensions. If None, Scott's rule.
    temporal_bandwidth : float, optional
        KDE bandwidth in hours for the time dimension. If None, Scott's rule.
    spatial_dedup_m : float
        Spatial deduplication threshold in metres (default 1000 m).
    temporal_dedup_s : float
        Temporal deduplication threshold in seconds (default 600 s).

    Returns
    -------
    CandidateGenerationResult
    """
    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    _validate_inputs(
        backward_result, n_candidates,
        spatial_bandwidth, temporal_bandwidth,
        spatial_dedup_m, temporal_dedup_s,
    )

    history = backward_result.trajectory_history
    detection_time = backward_result.detection_time
    horizon_hours = backward_result.horizon_hours

    # ------------------------------------------------------------------
    # Collect raw states
    # ------------------------------------------------------------------
    states = _collect_raw_states(history, detection_time)
    n_raw = len(states)

    # Compute bounds for filtering
    lat_min_raw = float(states[:, 0].min())
    lat_max_raw = float(states[:, 0].max())
    lon_min_raw = float(states[:, 1].min())
    lon_max_raw = float(states[:, 1].max())

    lat_min_bound = lat_min_raw - SPATIAL_MARGIN_DEG
    lat_max_bound = lat_max_raw + SPATIAL_MARGIN_DEG
    lon_min_bound = lon_min_raw - SPATIAL_MARGIN_DEG
    lon_max_bound = lon_max_raw + SPATIAL_MARGIN_DEG

    # ------------------------------------------------------------------
    # Build KDE
    # ------------------------------------------------------------------
    kde, scale = _build_kde(states, horizon_hours, spatial_bandwidth, temporal_bandwidth)

    # ------------------------------------------------------------------
    # Sample candidates with oversampling rounds
    # ------------------------------------------------------------------
    rng = np.random.default_rng(seed)
    all_candidates: List[CandidateSourceHypothesis] = []
    n_rejected_bounds = 0
    n_rejected_duplicates = 0

    for round_idx in range(MAX_OVERSAMPLE_ROUNDS):
        still_needed = n_candidates * OVERSAMPLE_FACTOR - len(all_candidates)
        if still_needed <= 0:
            break

        sample_count = max(still_needed, n_candidates * OVERSAMPLE_FACTOR)
        kde_seed = int(rng.integers(0, 2**31))

        # KDE returns shape (d, n) = (3, sample_count)
        scaled_samples = kde.resample(sample_count, seed=kde_seed)
        # Back-transform to physical units: shape (sample_count, 3)
        samples = (scaled_samples.T) * scale[np.newaxis, :]

        # Bounds filter
        mask = (
            (samples[:, 0] >= lat_min_bound) & (samples[:, 0] <= lat_max_bound) &
            (samples[:, 1] >= lon_min_bound) & (samples[:, 1] <= lon_max_bound) &
            (samples[:, 2] >= 0.0)           & (samples[:, 2] <= horizon_hours)
        )
        valid = samples[mask]
        n_rejected_bounds += int((~mask).sum())

        if len(valid) == 0:
            continue

        # Compute KDE density at valid samples (in scaled space)
        valid_scaled = valid / scale[np.newaxis, :]
        densities = kde.evaluate(valid_scaled.T)  # shape (n_valid,)

        # Build preliminary candidates
        for i in range(len(valid)):
            lat = float(valid[i, 0])
            lon = float(valid[i, 1])
            elapsed_h = float(valid[i, 2])
            density = float(densities[i]) if math.isfinite(float(densities[i])) else 0.0

            release_time, ts_index = _snap_to_step(elapsed_h, detection_time, history)

            cand = CandidateSourceHypothesis(
                candidate_id=0,       # assigned after dedup
                source_lat=lat,
                source_lon=lon,
                release_time=release_time,
                proposal_density=max(0.0, density),
                backward_support=0.0,  # filled after dedup
                timestamp_index=ts_index,
                source_generation_method=GENERATION_METHOD,
            )
            all_candidates.append(cand)

    # ------------------------------------------------------------------
    # Deduplicate (sort by density descending first)
    # ------------------------------------------------------------------
    all_candidates.sort(key=lambda c: c.proposal_density or 0.0, reverse=True)
    unique_candidates, n_dup = _deduplicate(all_candidates, spatial_dedup_m, temporal_dedup_s)
    n_rejected_duplicates = n_dup

    # Trim to requested count
    final_candidates = unique_candidates[:n_candidates]

    # ------------------------------------------------------------------
    # Assign IDs and compute backward support
    # ------------------------------------------------------------------
    for idx, cand in enumerate(final_candidates):
        cand.candidate_id = idx
        cand.backward_support = _compute_backward_support(
            cand.source_lat, cand.source_lon, cand.timestamp_index, history
        )

    # ------------------------------------------------------------------
    # Source statistics (diagnostics only)
    # ------------------------------------------------------------------
    if final_candidates:
        earliest = min(c.release_time for c in final_candidates)
        latest   = max(c.release_time for c in final_candidates)
        mean_density = float(np.mean([c.proposal_density or 0.0 for c in final_candidates]))
        mean_support = float(np.mean([c.backward_support for c in final_candidates]))
        stat_min_lat = min(c.source_lat for c in final_candidates)
        stat_max_lat = max(c.source_lat for c in final_candidates)
        stat_min_lon = min(c.source_lon for c in final_candidates)
        stat_max_lon = max(c.source_lon for c in final_candidates)
    else:
        earliest = latest = detection_time
        mean_density = mean_support = 0.0
        stat_min_lat = stat_max_lat = 0.0
        stat_min_lon = stat_max_lon = 0.0

    source_statistics: Dict[str, Any] = {
        "n_raw_states":           n_raw,
        "n_candidates_requested": n_candidates,
        "n_candidates_generated": len(final_candidates),
        "n_rejected_bounds":      n_rejected_bounds,
        "n_rejected_duplicates":  n_rejected_duplicates,
        "min_lat":                stat_min_lat,
        "max_lat":                stat_max_lat,
        "min_lon":                stat_min_lon,
        "max_lon":                stat_max_lon,
        "earliest_release_time":  earliest,
        "latest_release_time":    latest,
        "mean_proposal_density":  mean_density,
        "mean_backward_support":  mean_support,
    }

    return CandidateGenerationResult(
        candidates=final_candidates,
        n_candidates=len(final_candidates),
        detection_time=detection_time,
        horizon_hours=horizon_hours,
        source_statistics=source_statistics,
        generation_method=GENERATION_METHOD,
    )
