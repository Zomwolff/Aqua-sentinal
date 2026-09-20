"""
spatial_forward_adapter.py
===========================
Forward candidate simulation with spatially varying forcing.

APPROACH
========
simulate_ensemble() accepts a forcing_series list evaluated at a fixed point.
For spatially varying forcing we build a per-candidate forcing series at
the candidate's own source location using SpatialForcingField.get_series().

This is valid because:
  - Each candidate is a fixed-location point source.
  - The forcing experienced during transport from that source is best
    approximated by sampling the field at the candidate's release location
    for the full simulation duration.
  - simulate_ensemble() remains UNMODIFIED.

This is NOT the same as full Lagrangian spatial forcing (which would require
modifying simulate_ensemble), but it IS a material improvement over the
baseline where ALL candidates used forcing at the OBSERVATION centroid,
regardless of their own source location.

WHAT IS NOT CHANGED:
  - simulate_ensemble() — unchanged
  - particles.py — unchanged
  - forward_adapter.py — unchanged (baseline still usable)
  - All inference modules — unchanged
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from app.candidate_generation import CandidateSourceHypothesis
from app.forward_adapter import (
    ObservedSpill,
    CandidateSimulationResult,
    _validate_forcing_coverage,
    run_candidate,
)
from app.spatial_forcing_field import SpatialForcingField


def simulate_candidates_spatial(
    candidates: List[CandidateSourceHypothesis],
    detection_time: datetime,
    forcing_field: SpatialForcingField,
    observed_spill: ObservedSpill,
    n_particles: int = 100,
    seed: int = 42,
    area_m2: float = 1.0,
    dt_forcing_hours: float = 1.0,
) -> List[CandidateSimulationResult]:
    """
    Simulate all candidates using per-candidate spatially sampled forcing.

    For each candidate:
    1. Build a forcing_series at the candidate's (source_lat, source_lon)
       covering [candidate.release_time - 1h, detection_time + 1h].
    2. Call the existing run_candidate() with that local series.

    This means each candidate trajectory uses wind/current from its own
    release location rather than from the observation centroid.

    simulate_ensemble() is called unchanged inside run_candidate().

    Parameters
    ----------
    candidates : List[CandidateSourceHypothesis]
    detection_time : datetime (UTC-aware)
    forcing_field : SpatialForcingField
        Pre-built spatial forcing field covering the domain and time window.
    observed_spill : ObservedSpill
    n_particles : int
    seed : int
    area_m2 : float
    dt_forcing_hours : float
        Temporal resolution of the per-candidate forcing series (default 1 h).

    Returns
    -------
    List[CandidateSimulationResult] — same length and order as candidates.
    """
    results: List[CandidateSimulationResult] = []
    detection_utc = detection_time.astimezone(timezone.utc)

    for candidate in candidates:
        candidate_seed = seed + candidate.candidate_id

        # Buffer: add 1h before release and after detection for _interp_forcing boundary
        t_series_start = candidate.release_time - timedelta(hours=1)
        t_series_end   = detection_utc + timedelta(hours=1)

        try:
            # Build forcing series at candidate's source location
            local_series = forcing_field.get_series(
                lat=candidate.source_lat,
                lon=candidate.source_lon,
                t_start=t_series_start,
                t_end=t_series_end,
                dt_hours=dt_forcing_hours,
            )
        except Exception as e:
            results.append(CandidateSimulationResult(
                candidate_id=candidate.candidate_id,
                source_lat=candidate.source_lat,
                source_lon=candidate.source_lon,
                release_time=candidate.release_time,
                detection_time=detection_utc,
                duration_hours=(detection_utc - candidate.release_time).total_seconds() / 3600.0,
                success=False,
                error_type=type(e).__name__,
                error_message=f"Forcing series build failed: {e}",
                simulated_centroid_lat=None,
                simulated_centroid_lon=None,
                simulated_area_m2=None,
                simulated_polygon_wkt=None,
                simulated_prob50_wkt=None,
                simulated_prob90_wkt=None,
                comparison=None,
            ))
            continue

        # Use existing run_candidate() unchanged — just with a different forcing_series
        result = run_candidate(
            candidate=candidate,
            detection_time=detection_utc,
            forcing_series=local_series,
            observed_spill=observed_spill,
            n_particles=n_particles,
            seed=candidate_seed,
            area_m2=area_m2,
        )
        results.append(result)

    return results
