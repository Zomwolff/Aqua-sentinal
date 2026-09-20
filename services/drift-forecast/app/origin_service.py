"""Production origin-inference orchestration.

This module deliberately uses the same point-forcing interface as the existing
forward particle model. Validation-only spatial/HYCOM modules are not part of
the API path.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from app.backward_hindcast import backward_propagate
from app.bayesian_inference import compute_posterior
from app.candidate_generation import generate_candidates
from app.forward_adapter import ObservedSpill, simulate_candidates
from app.observation_likelihood import LikelihoodConfig
from app.origin_probability import OriginProbabilityResult, compute_origin_probability
from app.schemas import FootprintGeometry, _geom_centroid_and_area
from app.worker import MIN_FORCING_SAMPLES, _fetch_forcing_series

log = logging.getLogger(__name__)

# Production settings are server-side, not request parameters.
HORIZON_HOURS = 96.0
N_CANDIDATES = 100
N_PARTICLES_BWD = 300
N_PARTICLES_FWD = 100
SEED = 42
SIGMA_CENTROID_M = 5000.0
SIGMA_AREA_LOG = 1.0


class HistoricalForcingUnavailableError(RuntimeError):
    """The shared environmental pipeline has no usable forcing for this run."""


async def run_origin_inference(
    detection_time,
    observed_footprint: FootprintGeometry,
    forcing_pool,
) -> OriginProbabilityResult:
    """Run the production backward-to-forward Bayesian origin pipeline."""
    (centroid_lat, centroid_lon), area_m2 = _geom_centroid_and_area(observed_footprint)
    forcing_start = detection_time - timedelta(hours=HORIZON_HOURS)

    # Reuse the existing forward worker handoff unchanged.  The environmental
    # layer owns data acquisition and persistence; origin inference only asks
    # it for the standard particle-forcing representation.
    forcing_series = await _fetch_forcing_series(
        forcing_pool,
        centroid_lat,
        centroid_lon,
        detection_time,
        lookback_h=HORIZON_HOURS,
        lookahead_h=0.0,
    )
    if len(forcing_series) < MIN_FORCING_SAMPLES:
        raise HistoricalForcingUnavailableError(
            "The shared environmental_conditions forcing layer has "
            f"{len(forcing_series)} usable sample(s) for "
            f"{forcing_start.isoformat()} to {detection_time.isoformat()}; "
            f"{MIN_FORCING_SAMPLES} are required for origin inference."
        )

    polygon_wkt: Optional[str] = None
    try:
        from shapely.geometry import shape
        polygon_wkt = shape(observed_footprint.model_dump()).wkt
    except Exception:
        # Centroid/area likelihood is still valid without geometry-only metrics.
        log.warning("Could not convert observed footprint to WKT", exc_info=True)

    observed_spill = ObservedSpill(
        observed_lat=centroid_lat,
        observed_lon=centroid_lon,
        observed_area_m2=area_m2,
        detection_time=detection_time,
        polygon_wkt=polygon_wkt,
    )
    backward_result = backward_propagate(
        observed_lat=centroid_lat,
        observed_lon=centroid_lon,
        detection_time=detection_time,
        forcing_series=forcing_series,
        horizon_hours=HORIZON_HOURS,
        n_particles=N_PARTICLES_BWD,
        seed=SEED,
        observed_area_m2=area_m2,
    )
    candidates_result = generate_candidates(
        backward_result=backward_result,
        n_candidates=N_CANDIDATES,
        seed=SEED,
    )
    simulation_results = simulate_candidates(
        candidates=candidates_result.candidates,
        detection_time=detection_time,
        forcing_series=forcing_series,
        observed_spill=observed_spill,
        n_particles=N_PARTICLES_FWD,
        seed=SEED,
    )
    posterior = compute_posterior(
        simulation_results=simulation_results,
        proposal_densities=[c.proposal_density for c in candidates_result.candidates],
        detection_time=detection_time,
        config=LikelihoodConfig(
            sigma_centroid_m=SIGMA_CENTROID_M,
            sigma_area_log=SIGMA_AREA_LOG,
        ),
    )
    return compute_origin_probability(
        posterior=posterior,
        detection_time=detection_time,
        n_forcing_samples=len(forcing_series),
    )
