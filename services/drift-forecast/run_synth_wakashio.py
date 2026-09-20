"""
SYNTH-WAKASHIO-001  —  End-to-end spatial inference smoke test
==============================================================
Runs the frozen Experiment-C spatial pipeline on a synthetic upstream
SAR detection in the Wakashio domain.

Parameters are frozen server-side and NOT given to the pipeline:
  HORIZON_HOURS   = 96
  N_CANDIDATES    = 100
  N_PARTICLES_BWD = 300
  N_PARTICLES_FWD = 100
  SEED            = 42
  SIGMA_CENTROID_M = 5000
  SIGMA_AREA_LOG   = 1.0

Ground truth (hidden from inference, used only for post-hoc evaluation):
  True source lat  = -20.4417
  True source lon  =  57.7458
  True release     = 2020-08-06T01:37:55Z
  Transport        = 96 h
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ── FROZEN parameters (identical to Experiment C) ─────────────────────────────
HORIZON_HOURS    = 96.0
N_CANDIDATES     = 100
N_PARTICLES_BWD  = 300
N_PARTICLES_FWD  = 100
SEED             = 42
SIGMA_CENTROID_M = 5000.0
SIGMA_AREA_LOG   = 1.0

HYCOM_PATH = ROOT / "data" / "validation-data" / "hycom_wakashio_subset.nc"

# ── Observation inputs only (no ground truth) ─────────────────────────────────
DETECTION_TIME_ISO = "2020-08-10T01:37:55Z"
OBSERVED_FOOTPRINT = {
    "type": "Polygon",
    "coordinates": [[
        [57.7200, -20.4100],
        [57.7260, -20.4100],
        [57.7290, -20.4060],
        [57.7280, -20.4020],
        [57.7220, -20.4010],
        [57.7170, -20.4050],
        [57.7170, -20.4090],
        [57.7200, -20.4100],
    ]]
}

# ── Hidden ground truth — used ONLY for post-hoc evaluation ───────────────────
_TRUE_LAT  = -20.4417
_TRUE_LON  =  57.7458
_TRUE_RT   = datetime(2020, 8, 6, 1, 37, 55, tzinfo=timezone.utc)

# ──────────────────────────────────────────────────────────────────────────────

SEP = "=" * 72

print(SEP)
print("SYNTH-WAKASHIO-001  |  Frozen Experiment-C Spatial Pipeline")
print(SEP)
print(f"Detection time   : {DETECTION_TIME_ISO}")
print(f"Footprint        : Polygon, 8-point ring")
print(f"HYCOM subset     : {HYCOM_PATH}")
print(f"HYCOM exists     : {HYCOM_PATH.exists()}")
print(f"Frozen params    : HORIZON={HORIZON_HOURS}h  N_CAND={N_CANDIDATES}"
      f"  N_BWD={N_PARTICLES_BWD}  N_FWD={N_PARTICLES_FWD}  SEED={SEED}")
print()

assert HYCOM_PATH.exists(), f"HYCOM subset not found: {HYCOM_PATH}"

# ── 1. Schema validation + centroid/area derivation ───────────────────────────
from app.schemas import PolygonGeometry, _geom_centroid_and_area

t0_total = time.perf_counter()

detection_time = datetime.fromisoformat(DETECTION_TIME_ISO.replace("Z", "+00:00"))
footprint      = PolygonGeometry(**OBSERVED_FOOTPRINT)
(obs_lat, obs_lon), area_m2 = _geom_centroid_and_area(footprint)

print(f"Derived centroid : ({obs_lat:.5f}°N, {obs_lon:.5f}°E)")
print(f"Derived area     : {area_m2:,.0f} m²  ({area_m2/1e6:.3f} km²)")
print()

# ── 2. Forcing construction ────────────────────────────────────────────────────
print("── Step 2: Forcing construction (ERA5 grid + HYCOM loader) ──────────────")
print("   ERA5: live Open-Meteo calls for Wakashio domain, Aug 2020")
print("   HYCOM: loading from local NC file (no network download)")

from app.origin_service import build_spatial_forcing_field

t0_C = time.perf_counter()
forcing_field = build_spatial_forcing_field(
    centroid_lat=obs_lat,
    centroid_lon=obs_lon,
    area_m2=area_m2,
    detection_time=detection_time,
    horizon_hours=HORIZON_HOURS,
    hycom_path=HYCOM_PATH,
)
t1_C = time.perf_counter()
elapsed_C = t1_C - t0_C
print(f"   Forcing construction complete: {elapsed_C:.1f}s")
print()

# ── 3. Backward propagation ───────────────────────────────────────────────────
print("── Step 3: backward_propagate_spatial() ─────────────────────────────────")

from app.spatial_backward_hindcast import backward_propagate_spatial

t0_D = time.perf_counter()
backward_result = backward_propagate_spatial(
    observed_lat=obs_lat,
    observed_lon=obs_lon,
    detection_time=detection_time,
    forcing_field=forcing_field,
    horizon_hours=HORIZON_HOURS,
    n_particles=N_PARTICLES_BWD,
    seed=SEED,
    observed_area_m2=area_m2,
)
t1_D = time.perf_counter()
elapsed_D = t1_D - t0_D

fc = backward_result.final_centroid
rms = backward_result.uncertainty_metrics.get("rms_spread_m", float("nan"))
print(f"   Backward centroid : ({fc[0]:.4f}°N, {fc[1]:.4f}°E)")
print(f"   RMS spread        : {rms/1000:.2f} km")
print(f"   Elapsed           : {elapsed_D:.1f}s")
print()

# ── 4. Candidate generation ───────────────────────────────────────────────────
print("── Step 4: generate_candidates() ───────────────────────────────────────")

from app.candidate_generation import generate_candidates

t0_E = time.perf_counter()
candidates_result = generate_candidates(
    backward_result=backward_result,
    n_candidates=N_CANDIDATES,
    seed=SEED,
)
t1_E = time.perf_counter()
elapsed_E = t1_E - t0_E
n_cand = len(candidates_result.candidates)
print(f"   Candidates generated : {n_cand}")
print(f"   Elapsed              : {elapsed_E:.1f}s")
print()

# ── 5. Forward simulation ─────────────────────────────────────────────────────
print("── Step 5: simulate_candidates_spatial() ───────────────────────────────")

from app.spatial_forward_adapter import simulate_candidates_spatial
from app.forward_adapter import ObservedSpill

try:
    from shapely.geometry import shape as shp_shape
    polygon_wkt = shp_shape(OBSERVED_FOOTPRINT).wkt
except Exception:
    polygon_wkt = None

obs_spill = ObservedSpill(
    observed_lat=obs_lat,
    observed_lon=obs_lon,
    observed_area_m2=area_m2,
    detection_time=detection_time,
    polygon_wkt=polygon_wkt,
)

t0_F = time.perf_counter()
sim_results = simulate_candidates_spatial(
    candidates=candidates_result.candidates,
    detection_time=detection_time,
    forcing_field=forcing_field,
    observed_spill=obs_spill,
    n_particles=N_PARTICLES_FWD,
    seed=SEED,
)
t1_F = time.perf_counter()
elapsed_F = t1_F - t0_F

n_success = sum(1 for r in sim_results if r.success)
n_failed  = len(sim_results) - n_success
print(f"   Succeeded : {n_success}/{len(sim_results)}")
if n_failed:
    print(f"   Failed    : {n_failed}")
print(f"   Elapsed   : {elapsed_F:.1f}s")
print()

# ── 6. Bayesian posterior + origin probability ────────────────────────────────
print("── Step 6: compute_posterior() + compute_origin_probability() ──────────")

from app.bayesian_inference import compute_posterior
from app.observation_likelihood import LikelihoodConfig
from app.origin_probability import compute_origin_probability

proposal_densities = [c.proposal_density for c in candidates_result.candidates]

t0_G = time.perf_counter()
posterior = compute_posterior(
    simulation_results=sim_results,
    proposal_densities=proposal_densities,
    detection_time=detection_time,
    config=LikelihoodConfig(
        sigma_centroid_m=SIGMA_CENTROID_M,
        sigma_area_log=SIGMA_AREA_LOG,
    ),
)
origin_result = compute_origin_probability(
    posterior=posterior,
    detection_time=detection_time,
)
t1_G = time.perf_counter()
elapsed_G = t1_G - t0_G
t1_total = t1_G
print(f"   Elapsed : {elapsed_G:.3f}s")
print()

# ── 7. Serialize (API layer) ──────────────────────────────────────────────────
from app.origin_api import serialize_origin_result
result_dict = serialize_origin_result(origin_result)

# ── 8. Results ────────────────────────────────────────────────────────────────
sp = origin_result.spatial_posterior
tp = origin_result.temporal_posterior
d  = origin_result.diagnostics

from app.footprint_comparison import _haversine

map_spatial_err_m = _haversine(_TRUE_LAT, _TRUE_LON, sp.map_lat, sp.map_lon)
map_time_err_s    = abs((tp.map_release_time - _TRUE_RT).total_seconds())
map_time_err_h    = map_time_err_s / 3600.0

# Containment checks
def _in_spatial_cr(cr, true_lat, true_lon, threshold_m=20_000.0):
    for c in cr.candidates:
        if _haversine(true_lat, true_lon, c.source_lat, c.source_lon) <= threshold_m:
            return True
    return False

in_s50 = _in_spatial_cr(sp.credible_50, _TRUE_LAT, _TRUE_LON)
in_s90 = _in_spatial_cr(sp.credible_90, _TRUE_LAT, _TRUE_LON)
in_t50 = tp.credible_50_lo <= _TRUE_RT <= tp.credible_50_hi
in_t90 = tp.credible_90_lo <= _TRUE_RT <= tp.credible_90_hi

print(SEP)
print("INFERENCE RESULTS")
print(SEP)
print()
print("SPATIAL POSTERIOR:")
print(f"  MAP source          : ({sp.map_lat:.5f}°N, {sp.map_lon:.5f}°E)")
print(f"  Weighted centroid   : ({sp.weighted_centroid_lat:.5f}°N, {sp.weighted_centroid_lon:.5f}°E)")
print(f"  Weighted RMS spread : {sp.weighted_rms_spread_m/1000:.2f} km")
print(f"  50% CR extent       : {sp.credible_50.spatial_extent_km:.2f} km  ({sp.credible_50.n_candidates} candidates)")
print(f"  90% CR extent       : {sp.credible_90.spatial_extent_km:.2f} km  ({sp.credible_90.n_candidates} candidates)")
print()
print("TEMPORAL POSTERIOR:")
print(f"  MAP release time    : {tp.map_release_time.isoformat()}")
print(f"  Weighted mean       : {tp.weighted_mean_release_time.isoformat()}")
print(f"  50% CI              : {tp.credible_50_lo.isoformat()} → {tp.credible_50_hi.isoformat()}")
print(f"  90% CI              : {tp.credible_90_lo.isoformat()} → {tp.credible_90_hi.isoformat()}")
print(f"  Spread (90% width)  : {tp.spread_hours:.2f} h")
print()
print("DIAGNOSTICS:")
print(f"  ESS                 : {d.ess:.2f}  ({d.ess_ratio*100:.1f}%)")
print(f"  Max posterior wt    : {d.max_posterior_weight:.4f}")
print(f"  Nonzero candidates  : {d.n_nonzero_weight_candidates}")
print(f"  Significant (≥0.01) : {d.n_significant_candidates}")
print(f"  Quality flags       : {d.quality_flags if d.quality_flags else 'none'}")
print(f"  Warnings            : {d.warnings if d.warnings else 'none'}")
print()
print(SEP)
print("POST-HOC EVALUATION AGAINST HIDDEN TRUTH")
print(SEP)
print(f"  True source         : ({_TRUE_LAT}°N, {_TRUE_LON}°E)")
print(f"  True release time   : {_TRUE_RT.isoformat()}")
print(f"  True transport      : 96 h")
print()
print(f"  MAP spatial error   : {map_spatial_err_m/1000:.2f} km")
print(f"  MAP temporal error  : {map_time_err_h:.2f} h")
print(f"  True src in 50% CR  : {in_s50}  (threshold 20 km)")
print(f"  True src in 90% CR  : {in_s90}  (threshold 20 km)")
print(f"  True RT  in 50% CI  : {in_t50}")
print(f"  True RT  in 90% CI  : {in_t90}")
print()
print(SEP)
print("TIMING SUMMARY")
print(SEP)
print(f"  Forcing construction (ERA5 + HYCOM) : {elapsed_C:.1f} s")
print(f"  Backward propagation               : {elapsed_D:.1f} s")
print(f"  Candidate generation               : {elapsed_E:.1f} s")
print(f"  Forward simulation                 : {elapsed_F:.1f} s")
print(f"  Bayesian / origin probability      : {elapsed_G:.3f} s")
print(f"  Total inference time               : {t1_total - t0_C:.1f} s")
print(f"  Total from schema validation       : {t1_total - t0_total:.1f} s")
print()

# Save JSON result
out_path = ROOT / "results" / "synth_wakashio_001_result.json"
out_path.parent.mkdir(parents=True, exist_ok=True)
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump({
        "synth_case": "SYNTH-WAKASHIO-001",
        "detection_time": DETECTION_TIME_ISO,
        "observed_footprint": OBSERVED_FOOTPRINT,
        "derived_centroid_lat": obs_lat,
        "derived_centroid_lon": obs_lon,
        "derived_area_m2": area_m2,
        "pipeline": "frozen_experiment_c_spatial",
        "forcing": {
            "wind": "ERA5 spatially varying (Open-Meteo)",
            "current": "HYCOM GLBy0.08 Exp93.0 3-hourly (local NC)",
        },
        "frozen_params": {
            "HORIZON_HOURS": HORIZON_HOURS,
            "N_CANDIDATES": N_CANDIDATES,
            "N_PARTICLES_BWD": N_PARTICLES_BWD,
            "N_PARTICLES_FWD": N_PARTICLES_FWD,
            "SEED": SEED,
            "SIGMA_CENTROID_M": SIGMA_CENTROID_M,
            "SIGMA_AREA_LOG": SIGMA_AREA_LOG,
        },
        "inference_result": result_dict,
        "post_hoc_evaluation": {
            "true_source_lat": _TRUE_LAT,
            "true_source_lon": _TRUE_LON,
            "true_release_time": _TRUE_RT.isoformat(),
            "map_spatial_error_km": round(map_spatial_err_m / 1000.0, 3),
            "map_temporal_error_h": round(map_time_err_h, 3),
            "true_in_spatial_50cr": in_s50,
            "true_in_spatial_90cr": in_s90,
            "true_in_temporal_50ci": in_t50,
            "true_in_temporal_90ci": in_t90,
        },
        "timing_s": {
            "forcing_construction": round(elapsed_C, 1),
            "backward_propagation": round(elapsed_D, 1),
            "candidate_generation": round(elapsed_E, 1),
            "forward_simulation": round(elapsed_F, 1),
            "bayesian_inference": round(elapsed_G, 3),
            "total_inference": round(t1_total - t0_C, 1),
            "total_from_schema": round(t1_total - t0_total, 1),
        },
    }, fh, indent=2, default=str)

print(f"Full result saved to: {out_path}")
print(SEP)
print("SMOKE TEST COMPLETE")
print(SEP)
