"""
Step 5: Footprint Comparison — footprint_comparison.py

Computes quantitative mismatch metrics between a simulated oil-spill footprint
and an observed spill footprint.

SCIENTIFIC DISCLAIMER:
    The metrics produced here are raw physical mismatch quantities.
    They are NOT probabilities, likelihoods, or posterior scores.
    The likelihood function L(observation | θ) will be defined in Step 6.
    Do NOT interpret centroid_distance_m or relative_area_error as a probability.

POLYGON AVAILABILITY NOTE:
    In v1, the spill system provides centroid + area from SAR/optical detection.
    If polygon_wkt is None, IoU and Hausdorff distance cannot be computed
    and will be returned as None. This is expected behaviour, not an error.

HAUSDORFF APPROXIMATION:
    Shapely's hausdorff_distance() returns distance in the same units as the
    polygon coordinates, which are degrees here. We convert using the flat-Earth
    approximation 111,319 m/deg. This is accurate to ~1% at latitudes < 45°.
    For higher precision, convert to a projected coordinate system first.

WHAT THIS MODULE DOES NOT DO:
    - Does NOT compute likelihood
    - Does NOT compute posterior probability
    - Does NOT call simulate_ensemble()
    - Does NOT modify any existing file
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# shapely is in requirements.txt (>=2.0)
from shapely import wkt as shapely_wkt

# Flat-Earth approximation constant
M_PER_DEG: float = 111_319.0


# ---------------------------------------------------------------------------
# FootprintComparisonResult
# ---------------------------------------------------------------------------

@dataclass
class FootprintComparisonResult:
    """
    Raw physical mismatch metrics between a simulated and observed spill footprint.

    IMPORTANT: These are NOT probabilities, likelihoods, or posterior scores.
    They are observation-comparison quantities for use in likelihood computation
    (Step 6).

    Fields
    ------
    centroid_distance_m:
        Haversine distance between simulated centroid and observed centroid (m).
    observed_area_m2:
        Area of observed spill (m²).
    simulated_area_m2:
        Area of simulated spill at detection time (m²).
    area_difference_m2:
        |simulated_area_m2 - observed_area_m2| (m²).
    relative_area_error:
        area_difference_m2 / max(observed_area_m2, 1.0); dimensionless.
    iou:
        Intersection-over-union of observed and simulated polygons [0, 1].
        None if either polygon is unavailable.
    hausdorff_distance_m:
        Hausdorff distance between polygon boundaries (m), approximate.
        None if either polygon is unavailable.
    delta_lat_m:
        Signed north-south offset: (sim_lat - obs_lat) * 111319 m.
        Positive = simulated centroid is north of observed.
    delta_lon_m:
        Signed east-west offset: (sim_lon - obs_lon) * m_lon.
        Positive = simulated centroid is east of observed.
    valid_metrics:
        List of metric names that were successfully computed.
    comparison_metadata:
        Auxiliary information (polygon availability, approximation warnings).
    """
    centroid_distance_m:  float
    observed_area_m2:     float
    simulated_area_m2:    float
    area_difference_m2:   float
    relative_area_error:  float
    iou:                  Optional[float]
    hausdorff_distance_m: Optional[float]
    delta_lat_m:          float
    delta_lon_m:          float
    valid_metrics:        List[str]
    comparison_metadata:  Dict[str, Any]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance between two WGS-84 points in metres."""
    R = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    )
    return 2.0 * R * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _compute_iou(obs_wkt: str, sim_wkt: str) -> Optional[float]:
    """
    Intersection-over-Union of two WKT polygons.

    IoU = intersection_area / union_area
    Both areas are in degree² but the ratio is dimensionless and correct.
    Returns None on any parsing or geometry error.
    """
    try:
        obs_geom = shapely_wkt.loads(obs_wkt)
        sim_geom = shapely_wkt.loads(sim_wkt)
        intersection = obs_geom.intersection(sim_geom).area
        union = obs_geom.union(sim_geom).area
        if union <= 0.0:
            return 0.0
        return float(intersection / union)
    except Exception:
        return None


def _compute_hausdorff(obs_wkt: str, sim_wkt: str, obs_lat: float) -> Optional[float]:
    """
    Hausdorff distance between two WKT polygon boundaries (metres).

    APPROXIMATION: shapely.hausdorff_distance() returns degrees.
    Converts using 111,319 m/deg (flat-Earth approximation, ~1% error at lat<45°).
    Returns None on any parsing or geometry error.
    """
    try:
        obs_geom = shapely_wkt.loads(obs_wkt)
        sim_geom = shapely_wkt.loads(sim_wkt)
        hausdorff_deg = obs_geom.hausdorff_distance(sim_geom)
        return float(hausdorff_deg * M_PER_DEG)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def compare_footprints(
    observed_lat: float,
    observed_lon: float,
    observed_area_m2: float,
    simulated_centroid_lat: float,
    simulated_centroid_lon: float,
    simulated_area_m2: float,
    observed_polygon_wkt: Optional[str] = None,
    simulated_polygon_wkt: Optional[str] = None,
    simulated_prob50_wkt: Optional[str] = None,
) -> FootprintComparisonResult:
    """
    Compute quantitative mismatch between simulated and observed spill footprint.

    Parameters
    ----------
    observed_lat, observed_lon:
        Observed spill centroid (WGS-84 degrees).
    observed_area_m2:
        Observed spill area (m²).
    simulated_centroid_lat, simulated_centroid_lon:
        Simulated centroid from simulate_ensemble() (predicted_lat / predicted_lon).
    simulated_area_m2:
        Simulated physical area from simulate_ensemble() (physical_area_m2).
    observed_polygon_wkt:
        WKT polygon of observed spill. None if not available — IoU and Hausdorff
        will be None in that case. Do NOT fabricate geometry.
    simulated_polygon_wkt:
        WKT polygon from simulate_ensemble() (polygon_wkt key). None if not
        available.
    simulated_prob50_wkt:
        Optional 50% probability contour WKT for metadata.

    Returns
    -------
    FootprintComparisonResult
        All mismatch metrics. Fields that could not be computed are None.

    Scientific note:
        Results are observation-comparison metrics, NOT probabilities.
    """
    valid_metrics: List[str] = []
    metadata: Dict[str, Any] = {
        "observed_polygon_available": observed_polygon_wkt is not None,
        "simulated_polygon_available": simulated_polygon_wkt is not None,
        "hausdorff_approximation": "flat-Earth 111319 m/deg, ~1% error at lat<45°",
        "iou_note": "computed in degree² space; ratio is dimensionless and correct",
    }

    # ------------------------------------------------------------------
    # Centroid distance
    # ------------------------------------------------------------------
    centroid_distance_m = _haversine(
        observed_lat, observed_lon,
        simulated_centroid_lat, simulated_centroid_lon,
    )
    valid_metrics.append("centroid_distance_m")

    # ------------------------------------------------------------------
    # Area metrics
    # ------------------------------------------------------------------
    area_difference_m2 = abs(simulated_area_m2 - observed_area_m2)
    relative_area_error = area_difference_m2 / max(observed_area_m2, 1.0)
    valid_metrics.extend(["area_difference_m2", "relative_area_error"])

    # ------------------------------------------------------------------
    # Directional offset (signed, in metres)
    # ------------------------------------------------------------------
    m_lat = M_PER_DEG
    m_lon = m_lat * math.cos(math.radians(observed_lat)) + 1e-10
    delta_lat_m = (simulated_centroid_lat - observed_lat) * m_lat
    delta_lon_m = (simulated_centroid_lon - observed_lon) * m_lon
    valid_metrics.extend(["delta_lat_m", "delta_lon_m"])

    # ------------------------------------------------------------------
    # IoU (polygon-based)
    # ------------------------------------------------------------------
    iou: Optional[float] = None
    if observed_polygon_wkt is not None and simulated_polygon_wkt is not None:
        iou = _compute_iou(observed_polygon_wkt, simulated_polygon_wkt)
        if iou is not None:
            valid_metrics.append("iou")

    # ------------------------------------------------------------------
    # Hausdorff distance (polygon-based)
    # ------------------------------------------------------------------
    hausdorff_distance_m: Optional[float] = None
    if observed_polygon_wkt is not None and simulated_polygon_wkt is not None:
        hausdorff_distance_m = _compute_hausdorff(
            observed_polygon_wkt, simulated_polygon_wkt, observed_lat
        )
        if hausdorff_distance_m is not None:
            valid_metrics.append("hausdorff_distance_m")

    return FootprintComparisonResult(
        centroid_distance_m=centroid_distance_m,
        observed_area_m2=float(observed_area_m2),
        simulated_area_m2=float(simulated_area_m2),
        area_difference_m2=float(area_difference_m2),
        relative_area_error=float(relative_area_error),
        iou=iou,
        hausdorff_distance_m=hausdorff_distance_m,
        delta_lat_m=float(delta_lat_m),
        delta_lon_m=float(delta_lon_m),
        valid_metrics=valid_metrics,
        comparison_metadata=metadata,
    )
