"""
Step 5: Forward Adapter — forward_adapter.py

Wraps the existing Oil Spread V2 simulate_ensemble() for use in the hindcasting
pipeline, accepting a CandidateSourceHypothesis and running the forward model
from release_time to detection_time.

SCIENTIFIC DISCLAIMER:
    Results contain footprint comparison metrics, NOT probabilities or posterior
    scores. The likelihood function L(observation | θ) will be computed in
    Step 6 using these metrics and an observation-error model.

POINT-SOURCE APPROXIMATION:
    Candidates have (lat, lon, release_time) but no independently measured
    initial spill area. The default area_m2=1.0 is used as a point-source
    approximation. HOWEVER: the existing MIN_RADIUS_M = 500 m in particles.py
    means the initial particle distribution always has radius >= 500 m
    regardless of area_m2. Therefore area_m2=1.0 does NOT create a
    mathematically exact point source — this is a documented limitation.
    Do NOT modify MIN_RADIUS_M to change this behaviour.

DURATION CALCULATION:
    duration_hours = (detection_time - release_time).total_seconds() / 3600
    This is passed as horizons_h=[duration_hours] to simulate_ensemble().
    The default horizon list (1,3,6,12,24) is NEVER used — every candidate
    gets exactly its own duration.

FORCING COVERAGE:
    The adapter verifies that forcing_series covers [release_time, detection_time]
    before calling the forward model. It never silently uses the analytic fallback.

WHAT THIS MODULE DOES NOT DO:
    - Does NOT modify simulate_ensemble() or any existing physics file
    - Does NOT compute likelihood or Bayesian posterior
    - Does NOT use AIS or vessel attribution
    - Does NOT parallelize (sequential for correctness; optimize later)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from app.particles import simulate_ensemble
from app.candidate_generation import CandidateSourceHypothesis
from app.footprint_comparison import FootprintComparisonResult, compare_footprints


# ---------------------------------------------------------------------------
# ObservedSpill
# ---------------------------------------------------------------------------

@dataclass
class ObservedSpill:
    """
    Representation of the observed spill at detection time.

    NOTE on polygon availability:
        In v1, spill detection provides centroid + area from SAR/optical
        analysis. A WKT polygon is optional — if None, IoU and Hausdorff
        distance in comparison results will also be None. This is correct
        behaviour, not an error. Do NOT fabricate polygon geometry.
    """
    observed_lat:     float
    observed_lon:     float
    observed_area_m2: float
    detection_time:   datetime
    polygon_wkt:      Optional[str] = None


# ---------------------------------------------------------------------------
# CandidateSimulationResult
# ---------------------------------------------------------------------------

@dataclass
class CandidateSimulationResult:
    """
    Result of forward-simulating one candidate and comparing to the observation.

    IMPORTANT: The 'comparison' field contains raw mismatch metrics, NOT
    probabilities or posterior scores. See FootprintComparisonResult for details.

    Fields
    ------
    candidate_id, source_lat, source_lon, release_time:
        Identity of the candidate θ — preserved for later inference association.
    success:
        True if simulate_ensemble() completed without error.
    error_type, error_message:
        Exception type name and message if success=False; None otherwise.
    simulated_centroid_lat / lon:
        Centroid of forward simulation at detection_time (predicted_lat/lon).
    simulated_area_m2:
        Physical oil spread area at detection_time (physical_area_m2 from Fay model).
    simulated_polygon_wkt:
        WKT polygon of physical spread at detection_time (polygon_wkt key).
    simulated_prob50_wkt, simulated_prob90_wkt:
        Probability contour WKTs if available.
    comparison:
        FootprintComparisonResult — None if simulation failed.
    """
    candidate_id:           int
    source_lat:             float
    source_lon:             float
    release_time:           datetime
    detection_time:         datetime
    duration_hours:         float
    success:                bool
    error_type:             Optional[str]
    error_message:          Optional[str]
    simulated_centroid_lat: Optional[float]
    simulated_centroid_lon: Optional[float]
    simulated_area_m2:      Optional[float]
    simulated_polygon_wkt:  Optional[str]
    simulated_prob50_wkt:   Optional[str]
    simulated_prob90_wkt:   Optional[str]
    comparison:             Optional[FootprintComparisonResult]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_forcing_coverage(
    forcing_series: Sequence[Dict[str, Any]],
    release_time: datetime,
    detection_time: datetime,
) -> None:
    """
    Verify that forcing_series contains samples covering [release_time, detection_time].

    Raises ValueError if no sample exists at or after release_time, or if no
    sample exists at or before detection_time. This prevents silent use of
    persistence forcing from entirely outside the required window.
    """
    if not forcing_series:
        raise ValueError("forcing_series is empty — cannot run forward simulation")

    times = [s["t"] for s in forcing_series]

    # Ensure at least one sample exists in the valid range
    has_sample_after_release = any(t >= release_time for t in times)
    has_sample_before_detection = any(t <= detection_time for t in times)

    if not has_sample_after_release:
        raise ValueError(
            f"No forcing samples at or after release_time={release_time.isoformat()}. "
            f"Forcing range: [{min(times).isoformat()}, {max(times).isoformat()}]"
        )
    if not has_sample_before_detection:
        raise ValueError(
            f"No forcing samples at or before detection_time={detection_time.isoformat()}. "
            f"Forcing range: [{min(times).isoformat()}, {max(times).isoformat()}]"
        )


# ---------------------------------------------------------------------------
# Single candidate forward simulation
# ---------------------------------------------------------------------------

def run_candidate(
    candidate: CandidateSourceHypothesis,
    detection_time: datetime,
    forcing_series: Sequence[Dict[str, Any]],
    observed_spill: ObservedSpill,
    n_particles: int = 200,
    seed: int = 42,
    area_m2: float = 1.0,
) -> CandidateSimulationResult:
    """
    Forward-simulate one candidate source hypothesis and compare to observation.

    The existing simulate_ensemble() is called with horizons_h=[duration_hours]
    where duration_hours = (detection_time - release_time).total_seconds() / 3600.
    This gives exactly one output horizon corresponding to the detection time.

    Parameters
    ----------
    candidate : CandidateSourceHypothesis
        The candidate θ = (source_lat, source_lon, release_time) from Step 4.
    detection_time : datetime
        The observation time (UTC-aware).
    forcing_series : Sequence[Dict]
        Historical forcing from load_historical_forcing() covering the window.
        Must include at least one sample in [release_time, detection_time].
    observed_spill : ObservedSpill
        Observed spill representation for comparison.
    n_particles : int
        Particle count for forward simulation (default 200 — lower than the
        backward 500 to keep batch runtime manageable).
    seed : int
        RNG seed for the forward simulation.
    area_m2 : float
        Initial spill area. Default 1.0 (point-source approximation).
        Note: MIN_RADIUS_M=500m in particles.py means initial disc is always
        ≥500 m radius regardless. This does not produce an exact point source.

    Returns
    -------
    CandidateSimulationResult
        success=True if simulation completed; success=False otherwise.
        Failure is isolated — does NOT propagate to the batch.
    """
    # Compute duration
    duration_hours = (
        detection_time - candidate.release_time
    ).total_seconds() / 3600.0

    # Validate duration before any expensive work
    if duration_hours <= 0.0:
        return CandidateSimulationResult(
            candidate_id=candidate.candidate_id,
            source_lat=candidate.source_lat,
            source_lon=candidate.source_lon,
            release_time=candidate.release_time,
            detection_time=detection_time,
            duration_hours=duration_hours,
            success=False,
            error_type="ValueError",
            error_message=(
                f"release_time ({candidate.release_time.isoformat()}) must be "
                f"before detection_time ({detection_time.isoformat()}); "
                f"duration_hours={duration_hours:.4f}"
            ),
            simulated_centroid_lat=None,
            simulated_centroid_lon=None,
            simulated_area_m2=None,
            simulated_polygon_wkt=None,
            simulated_prob50_wkt=None,
            simulated_prob90_wkt=None,
            comparison=None,
        )

    try:
        # Validate forcing coverage before calling forward model
        _validate_forcing_coverage(
            forcing_series, candidate.release_time, detection_time
        )

        # ------------------------------------------------------------------
        # Run the EXISTING forward model
        # CRITICAL: pass horizons_h=[duration_hours] — NOT the default list
        # ------------------------------------------------------------------
        sim_results = simulate_ensemble(
            spill_lat=candidate.source_lat,
            spill_lon=candidate.source_lon,
            area_m2=area_m2,
            start_ts=candidate.release_time,
            forcing_series=forcing_series,
            horizons_h=[duration_hours],  # ← exactly one horizon = duration
            n_particles=n_particles,
            seed=seed,
        )

        # simulate_ensemble returns one dict per horizon; we requested exactly one
        fc = sim_results[0]

        # ------------------------------------------------------------------
        # Compare simulated footprint to observation
        # ------------------------------------------------------------------
        comparison = compare_footprints(
            observed_lat=observed_spill.observed_lat,
            observed_lon=observed_spill.observed_lon,
            observed_area_m2=observed_spill.observed_area_m2,
            simulated_centroid_lat=fc["predicted_lat"],
            simulated_centroid_lon=fc["predicted_lon"],
            simulated_area_m2=fc["physical_area_m2"],
            observed_polygon_wkt=observed_spill.polygon_wkt,
            simulated_polygon_wkt=fc.get("polygon_wkt"),
            simulated_prob50_wkt=fc.get("probability_50_wkt"),
        )

        return CandidateSimulationResult(
            candidate_id=candidate.candidate_id,
            source_lat=candidate.source_lat,
            source_lon=candidate.source_lon,
            release_time=candidate.release_time,
            detection_time=detection_time,
            duration_hours=duration_hours,
            success=True,
            error_type=None,
            error_message=None,
            simulated_centroid_lat=fc["predicted_lat"],
            simulated_centroid_lon=fc["predicted_lon"],
            simulated_area_m2=fc["physical_area_m2"],
            simulated_polygon_wkt=fc.get("polygon_wkt"),
            simulated_prob50_wkt=fc.get("probability_50_wkt"),
            simulated_prob90_wkt=fc.get("probability_90_wkt"),
            comparison=comparison,
        )

    except Exception as e:
        return CandidateSimulationResult(
            candidate_id=candidate.candidate_id,
            source_lat=candidate.source_lat,
            source_lon=candidate.source_lon,
            release_time=candidate.release_time,
            detection_time=detection_time,
            duration_hours=duration_hours,
            success=False,
            error_type=type(e).__name__,
            error_message=str(e),
            simulated_centroid_lat=None,
            simulated_centroid_lon=None,
            simulated_area_m2=None,
            simulated_polygon_wkt=None,
            simulated_prob50_wkt=None,
            simulated_prob90_wkt=None,
            comparison=None,
        )


# ---------------------------------------------------------------------------
# Batch candidate simulation
# ---------------------------------------------------------------------------

def simulate_candidates(
    candidates: List[CandidateSourceHypothesis],
    detection_time: datetime,
    forcing_series: Sequence[Dict[str, Any]],
    observed_spill: ObservedSpill,
    n_particles: int = 200,
    seed: int = 42,
    area_m2: float = 1.0,
) -> List[CandidateSimulationResult]:
    """
    Sequentially simulate all candidates and compare each to the observation.

    Per-candidate seed = seed + candidate.candidate_id to ensure diversity
    while maintaining reproducibility (same global seed → identical batch).

    One candidate failure does NOT stop the batch. All results are returned
    in the same order as the input list, preserving candidate identity.

    Parameters
    ----------
    candidates : List[CandidateSourceHypothesis]
        Output of generate_candidates() from Step 4.
    detection_time : datetime
        The observation time (UTC-aware).
    forcing_series : Sequence[Dict]
        Historical forcing covering at least [earliest release_time, detection_time].
    observed_spill : ObservedSpill
        Observed spill representation for comparison.
    n_particles : int
        Forward simulation particle count per candidate (default 200).
    seed : int
        Global RNG seed base.
    area_m2 : float
        Initial spill area for all candidates (default 1.0 = point-source approx).

    Returns
    -------
    List[CandidateSimulationResult]
        Same length as candidates, in the same order.
    """
    results: List[CandidateSimulationResult] = []
    for candidate in candidates:
        candidate_seed = seed + candidate.candidate_id
        result = run_candidate(
            candidate=candidate,
            detection_time=detection_time,
            forcing_series=forcing_series,
            observed_spill=observed_spill,
            n_particles=n_particles,
            seed=candidate_seed,
            area_m2=area_m2,
        )
        results.append(result)
    return results
