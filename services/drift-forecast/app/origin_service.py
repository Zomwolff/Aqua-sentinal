"""Production origin-inference orchestration.

Environmental forcing boundary
-------------------------------
The origin module does NOT query the live ``environmental_conditions``
database.  That table holds near-real-time snapshots only and has no
records for historical detection times (e.g. the 2020 Wakashio event).

Instead, forcing is obtained from Open-Meteo's public archive APIs via
``load_historical_forcing()`` (forcing_loader.py):
  - Wind:    archive-api.open-meteo.com  (ERA5 reanalysis, back to 1940)
  - Current: marine-api.open-meteo.com  (hourly, same historical coverage)

Both recent and historical detections go through the same path.  The
loader already handles the near-future guard (rejects t_end < 1 day ago)
and returns the standard ``forcing_series`` list consumed by the existing
``backward_propagate`` → ``simulate_candidates`` pipeline unchanged.

Nothing below the forcing-acquisition line is modified.
"""
from __future__ import annotations

import logging
from datetime import timedelta, timezone
from typing import Optional

from app.backward_hindcast import backward_propagate
from app.bayesian_inference import compute_posterior
from app.candidate_generation import generate_candidates
from app.forcing_loader import load_historical_forcing, ForcingLoaderError
from app.forward_adapter import ObservedSpill, simulate_candidates
from app.observation_likelihood import LikelihoodConfig
from app.origin_probability import OriginProbabilityResult, compute_origin_probability
from app.schemas import FootprintGeometry, _geom_centroid_and_area

log = logging.getLogger(__name__)

# ── Frozen server-side inference parameters ──────────────────────────────────
# Clients cannot override these via the request body.
HORIZON_HOURS:     float = 96.0
N_CANDIDATES:      int   = 100
N_PARTICLES_BWD:   int   = 300
N_PARTICLES_FWD:   int   = 100
SEED:              int   = 42
SIGMA_CENTROID_M:  float = 5_000.0
SIGMA_AREA_LOG:    float = 1.0

# Minimum forcing samples required to run the ensemble.
# load_historical_forcing() raises InsufficientDataError when it cannot
# meet this (the caller will see that as a job failure with a clear message).
_MIN_FORCING_SAMPLES = 2


class HistoricalForcingUnavailableError(RuntimeError):
    """Raised when Open-Meteo cannot provide enough forcing for this window.

    This replaces the old error that fired when environmental_conditions had
    no rows — the cause and fix are now different, but we keep the same
    exception name so any existing error-handling code still matches.
    """


def run_origin_inference(
    detection_time,
    observed_footprint: FootprintGeometry,
) -> OriginProbabilityResult:
    """Run the production backward-to-forward Bayesian origin pipeline.

    Steps
    -----
    1. Obtain ``forcing_series`` from Open-Meteo archive for the 96-hour
       window ending at ``detection_time``.  Works for any past date.
    2. Run the committed inference pipeline unchanged:
       backward_propagate → generate_candidates → simulate_candidates
       → compute_posterior → compute_origin_probability
    """
    (centroid_lat, centroid_lon), area_m2 = _geom_centroid_and_area(observed_footprint)

    # ── Step 1: fetch forcing from Open-Meteo (ERA5 wind + marine current) ──
    t_end   = detection_time.astimezone(timezone.utc)
    t_start = t_end - timedelta(hours=HORIZON_HOURS)

    try:
        forcing_series = load_historical_forcing(
            lat=centroid_lat,
            lon=centroid_lon,
            t_start=t_start,
            t_end=t_end,
            min_samples=_MIN_FORCING_SAMPLES,
        )
    except ForcingLoaderError as exc:
        raise HistoricalForcingUnavailableError(
            f"Open-Meteo could not provide forcing for "
            f"{t_start.isoformat()} → {t_end.isoformat()} "
            f"at ({centroid_lat:.4f}, {centroid_lon:.4f}): {exc}"
        ) from exc

    log.info(
        "Origin forcing obtained: %d samples, %s → %s",
        len(forcing_series),
        forcing_series[0]["t"].isoformat(),
        forcing_series[-1]["t"].isoformat(),
    )

    # ── Step 2: build observed-spill descriptor ──────────────────────────────
    polygon_wkt: Optional[str] = None
    try:
        from shapely.geometry import shape
        polygon_wkt = shape(observed_footprint.model_dump()).wkt
    except Exception:
        log.warning("Could not convert observed footprint to WKT", exc_info=True)

    observed_spill = ObservedSpill(
        observed_lat=centroid_lat,
        observed_lon=centroid_lon,
        observed_area_m2=area_m2,
        detection_time=detection_time,
        polygon_wkt=polygon_wkt,
    )

    # ── Step 3: backward propagation ────────────────────────────────────────
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

    # ── Step 4: candidate generation ────────────────────────────────────────
    candidates_result = generate_candidates(
        backward_result=backward_result,
        n_candidates=N_CANDIDATES,
        seed=SEED,
    )

    # ── Step 5: forward candidate simulation ────────────────────────────────
    simulation_results = simulate_candidates(
        candidates=candidates_result.candidates,
        detection_time=detection_time,
        forcing_series=forcing_series,
        observed_spill=observed_spill,
        n_particles=N_PARTICLES_FWD,
        seed=SEED,
    )

    # ── Step 6: Bayesian inference ───────────────────────────────────────────
    posterior = compute_posterior(
        simulation_results=simulation_results,
        proposal_densities=[c.proposal_density for c in candidates_result.candidates],
        detection_time=detection_time,
        config=LikelihoodConfig(
            sigma_centroid_m=SIGMA_CENTROID_M,
            sigma_area_log=SIGMA_AREA_LOG,
        ),
    )

    # ── Step 7: origin probability ───────────────────────────────────────────
    return compute_origin_probability(
        posterior=posterior,
        detection_time=detection_time,
        n_forcing_samples=len(forcing_series),
    )
