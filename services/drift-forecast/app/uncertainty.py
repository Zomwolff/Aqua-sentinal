"""
Uncertainty representation from Lagrangian particle ensemble.

This module handles:
- Particle density estimation (2D KDE or grid-based)
- Probability contour extraction (50%, 90%)
- Uncertainty metrics (RMS spread, ensemble variance)

IMPORTANT:
- Particle dispersion represents FORECAST UNCERTAINTY, not physical oil spreading
- Particle spread = transport uncertainty + diffusion + physical spreading
- RMS particle distance ≠ physical oil slick radius
- Convex hull ≠ probability contour

The particle ensemble captures:
1. Uncertainty in environmental forcing (wind/current interpolation)
2. Sub-grid-scale turbulent diffusion (Fickian random walk)
3. Model uncertainty (parameterizations)
"""
from __future__ import annotations
import math
from typing import Dict, List, Tuple, Optional
import numpy as np
from scipy.stats import gaussian_kde
from scipy.spatial import ConvexHull


def compute_particle_statistics(
    lat_particles: np.ndarray,
    lon_particles: np.ndarray,
    centroid_lat: float,
    centroid_lon: float,
) -> Dict[str, float]:
    """
    Compute uncertainty metrics from particle ensemble.
    
    Parameters:
        lat_particles: Particle latitudes (N,)
        lon_particles: Particle longitudes (N,)
        centroid_lat: Ensemble mean latitude
        centroid_lon: Ensemble mean longitude
    
    Returns:
        Dictionary with:
        - rms_spread_m: Root-mean-square particle distance from centroid (metres)
        - std_east_m: Standard deviation in east direction
        - std_north_m: Standard deviation in north direction
    
    INTERPRETATION:
    - rms_spread_m represents ensemble uncertainty, NOT physical oil radius
    - For Gaussian-distributed particles, ~68% fall within 1 RMS
    - ~95% fall within 2 RMS
    """
    m_lat = 111_319.0  # metres per degree latitude
    m_lon = 111_319.0 * math.cos(math.radians(centroid_lat)) + 1e-10
    
    # Distance from centroid in metres
    dlat_m = (lat_particles - centroid_lat) * m_lat
    dlon_m = (lon_particles - centroid_lon) * m_lon
    distances_m = np.sqrt(dlat_m**2 + dlon_m**2)
    
    rms_spread_m = float(np.sqrt(np.mean(distances_m**2)))
    std_east_m = float(np.std(dlon_m))
    std_north_m = float(np.std(dlat_m))
    
    return {
        "rms_spread_m": rms_spread_m,
        "std_east_m": std_east_m,
        "std_north_m": std_north_m,
    }


def compute_probability_contours_kde(
    lat_particles: np.ndarray,
    lon_particles: np.ndarray,
    probability_levels: List[float] = [0.50, 0.90],
    grid_resolution: int = 50,
) -> Dict[str, str]:
    """
    Compute probability contours using Kernel Density Estimation.
    
    Parameters:
        lat_particles: Particle latitudes
        lon_particles: Particle longitudes
        probability_levels: List of probability levels (e.g., [0.50, 0.90])
        grid_resolution: Grid size for contour extraction
    
    Returns:
        Dictionary mapping probability level to WKT POLYGON string
        Example: {"0.50": "POLYGON(...)", "0.90": "POLYGON(...)"}
    
    SCIENTIFIC INTERPRETATION:
    - 50% contour encloses approximately 50% of particle probability mass (geometric approximation, ±5% typical error)
    - 90% contour encloses approximately 90% of particle probability mass (geometric approximation, ±5% typical error)
    - Convex hull approximation introduces geometric error compared to exact contours
    - These represent forecast uncertainty, NOT confirmed spill boundaries
    """
    if len(lat_particles) < 10:
        # Not enough particles for meaningful KDE
        return {}
    
    try:
        # Create KDE
        positions = np.vstack([lon_particles, lat_particles])
        kernel = gaussian_kde(positions)
        
        # Create evaluation grid
        lon_min, lon_max = lon_particles.min(), lon_particles.max()
        lat_min, lat_max = lat_particles.min(), lat_particles.max()
        
        # Add margin
        lon_range = lon_max - lon_min
        lat_range = lat_max - lat_min
        margin_lon = max(0.01, lon_range * 0.2)
        margin_lat = max(0.01, lat_range * 0.2)
        
        lon_grid = np.linspace(lon_min - margin_lon, lon_max + margin_lon, grid_resolution)
        lat_grid = np.linspace(lat_min - margin_lat, lat_max + margin_lat, grid_resolution)
        lon_mesh, lat_mesh = np.meshgrid(lon_grid, lat_grid)
        
        # Evaluate density
        grid_positions = np.vstack([lon_mesh.ravel(), lat_mesh.ravel()])
        density = kernel(grid_positions).reshape(lon_mesh.shape)
        
        # Normalize to probability (integrate to 1)
        cell_area = (lon_grid[1] - lon_grid[0]) * (lat_grid[1] - lat_grid[0])
        density_normalized = density / (density.sum() * cell_area)
        
        # Sort by density descending
        flat_density = density_normalized.ravel()
        flat_lon = lon_mesh.ravel()
        flat_lat = lat_mesh.ravel()
        
        sorted_indices = np.argsort(flat_density)[::-1]
        sorted_density = flat_density[sorted_indices]
        sorted_lon = flat_lon[sorted_indices]
        sorted_lat = flat_lat[sorted_indices]
        
        # Compute cumulative probability mass
        cumsum_prob = np.cumsum(sorted_density) * cell_area
        
        contours = {}
        for prob_level in probability_levels:
            # Find threshold density that encloses prob_level of mass
            threshold_idx = np.searchsorted(cumsum_prob, prob_level)
            if threshold_idx >= len(sorted_density):
                threshold_idx = len(sorted_density) - 1
            
            threshold_density = sorted_density[threshold_idx]
            
            # Extract points above threshold
            mask = density_normalized >= threshold_density
            contour_lon = lon_mesh[mask]
            contour_lat = lat_mesh[mask]
            
            if len(contour_lon) >= 4:
                # Compute convex hull of high-density region
                points = np.column_stack([contour_lon, contour_lat])
                try:
                    hull = ConvexHull(points)
                    hull_points = points[hull.vertices]
                    # Close the polygon
                    hull_points = np.vstack([hull_points, hull_points[0:1]])
                    
                    coords = ", ".join(f"{lon:.6f} {lat:.6f}" for lon, lat in hull_points)
                    wkt = f"POLYGON(({coords}))"
                    contours[f"{prob_level:.2f}"] = wkt
                except Exception:
                    # Hull computation failed
                    pass
        
        return contours
    
    except Exception as e:
        # KDE failed, return empty
        return {}


def compute_probability_contours_grid(
    lat_particles: np.ndarray,
    lon_particles: np.ndarray,
    probability_levels: List[float] = [0.50, 0.90],
    grid_resolution: int = 40,
    smoothing_sigma: float = 1.0,
) -> Dict[str, str]:
    """
    Compute probability contours using normalized particle density grid.
    
    This is a computationally simpler alternative to KDE.
    
    Parameters:
        lat_particles: Particle latitudes
        lon_particles: Particle longitudes
        probability_levels: List of probability levels
        grid_resolution: Grid cells per axis
        smoothing_sigma: Gaussian smoothing width (grid cells)
    
    Returns:
        Dictionary mapping probability level to WKT POLYGON
    """
    if len(lat_particles) < 10:
        return {}
    
    try:
        # Create 2D histogram (particle density grid)
        lon_min, lon_max = lon_particles.min(), lon_particles.max()
        lat_min, lat_max = lat_particles.min(), lat_particles.max()
        
        lon_range = max(lon_max - lon_min, 0.01)
        lat_range = max(lat_max - lat_min, 0.01)
        margin = 0.2
        
        lon_bins = np.linspace(lon_min - margin * lon_range, lon_max + margin * lon_range, grid_resolution + 1)
        lat_bins = np.linspace(lat_min - margin * lat_range, lat_max + margin * lat_range, grid_resolution + 1)
        
        hist, lon_edges, lat_edges = np.histogram2d(
            lon_particles, lat_particles,
            bins=[lon_bins, lat_bins],
        )
        
        # Smooth with Gaussian filter if scipy is available
        try:
            from scipy.ndimage import gaussian_filter
            hist_smooth = gaussian_filter(hist, sigma=smoothing_sigma)
        except ImportError:
            hist_smooth = hist
        
        # Normalize to probability (sum to 1)
        hist_prob = hist_smooth / hist_smooth.sum()
        
        # Sort by probability descending
        flat_prob = hist_prob.ravel()
        sorted_indices = np.argsort(flat_prob)[::-1]
        sorted_prob = flat_prob[sorted_indices]
        
        # Cumulative probability
        cumsum_prob = np.cumsum(sorted_prob)
        
        # Grid centers
        lon_centers = (lon_edges[:-1] + lon_edges[1:]) / 2
        lat_centers = (lat_edges[:-1] + lat_edges[1:]) / 2
        lon_mesh, lat_mesh = np.meshgrid(lon_centers, lat_centers, indexing='ij')
        
        contours = {}
        for prob_level in probability_levels:
            # Find threshold
            threshold_idx = np.searchsorted(cumsum_prob, prob_level)
            if threshold_idx >= len(sorted_prob):
                threshold_idx = len(sorted_prob) - 1
            threshold_prob = sorted_prob[threshold_idx]
            
            # Extract high-density cells
            mask = hist_prob >= threshold_prob
            contour_lon = lon_mesh[mask]
            contour_lat = lat_mesh[mask]
            
            if len(contour_lon) >= 4:
                points = np.column_stack([contour_lon, contour_lat])
                try:
                    hull = ConvexHull(points)
                    hull_points = points[hull.vertices]
                    hull_points = np.vstack([hull_points, hull_points[0:1]])
                    
                    coords = ", ".join(f"{lon:.6f} {lat:.6f}" for lon, lat in hull_points)
                    wkt = f"POLYGON(({coords}))"
                    contours[f"{prob_level:.2f}"] = wkt
                except Exception:
                    pass
        
        return contours
    
    except Exception:
        return {}


def compute_uncertainty_footprint(
    lat_particles: np.ndarray,
    lon_particles: np.ndarray,
    method: str = "grid",  # "kde" or "grid"
    probability_levels: List[float] = [0.50, 0.90],
) -> Dict:
    """
    Compute complete uncertainty representation from particle ensemble.
    
    Parameters:
        lat_particles: Particle latitudes
        lon_particles: Particle longitudes
        method: "kde" or "grid" for probability contour extraction
        probability_levels: List of probability levels
    
    Returns:
        Dictionary with:
        - centroid_lat, centroid_lon: Ensemble mean position
        - rms_spread_m: RMS uncertainty radius
        - std_east_m, std_north_m: Directional uncertainties
        - probability_contours: Dict mapping level to WKT polygon
        - convex_hull_wkt: Convex hull of all particles (for reference, NOT a probability contour)
    """
    if len(lat_particles) < 4:
        return {
            "centroid_lat": float(np.mean(lat_particles)) if len(lat_particles) > 0 else 0.0,
            "centroid_lon": float(np.mean(lon_particles)) if len(lon_particles) > 0 else 0.0,
            "rms_spread_m": 0.0,
            "probability_contours": {},
            "convex_hull_wkt": None,
        }
    
    # Centroid
    centroid_lat = float(np.mean(lat_particles))
    centroid_lon = float(np.mean(lon_particles))
    
    # Statistics
    stats = compute_particle_statistics(lat_particles, lon_particles, centroid_lat, centroid_lon)
    
    # Probability contours
    if method == "kde":
        prob_contours = compute_probability_contours_kde(
            lat_particles, lon_particles, probability_levels
        )
    else:
        prob_contours = compute_probability_contours_grid(
            lat_particles, lon_particles, probability_levels
        )
    
    # Convex hull (for reference/visualization, NOT a probability interpretation)
    hull_wkt = None
    try:
        points = np.column_stack([lon_particles, lat_particles])
        unique_points = np.unique(points, axis=0)
        if len(unique_points) >= 4:
            hull = ConvexHull(unique_points)
            hull_points = unique_points[hull.vertices]
            hull_points = np.vstack([hull_points, hull_points[0:1]])
            coords = ", ".join(f"{lon:.6f} {lat:.6f}" for lon, lat in hull_points)
            hull_wkt = f"POLYGON(({coords}))"
    except Exception:
        pass
    
    return {
        "centroid_lat": centroid_lat,
        "centroid_lon": centroid_lon,
        "rms_spread_m": stats["rms_spread_m"],
        "std_east_m": stats["std_east_m"],
        "std_north_m": stats["std_north_m"],
        "probability_contours": prob_contours,
        "convex_hull_wkt": hull_wkt,
    }
