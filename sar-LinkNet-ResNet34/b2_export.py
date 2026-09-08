"""B2 Look-Alike Classifier — candidate feature CSV export (PART 16).

Reads one or more `{stem}_spill_meta.json` files produced by geo_postprocess.py
and writes one row per CANDIDATE (never per pixel):

    candidate_id, scene_id, source_id, label, label_name, category,
    scene_datetime, centroid_lat, centroid_lon,
    mean_backscatter, std_backscatter,
    glcm_contrast, glcm_homogeneity, glcm_energy, glcm_correlation,
    area_km2, perimeter_m, elongation, boundary_irregularity, edge_sharpness,
    wind_speed_kmh, distance_to_nearest_vessel_km, persistence_count

Conventions (do not "complete" the matrix):
  - Missing features are written as EMPTY fields, never 0. A missing
    contextual value (no weather/AIS/other scenes) is informative absence.
  - Metadata columns (candidate_id, scene_id, source_id, coordinates, labels)
    are exported for bookkeeping but must NOT enter the Random Forest matrix.
  - Units: area_km2 (from area_m2), perimeter_m (metric), wind_speed_kmh,
    distance_to_nearest_vessel_km. GLCM columns are the *_mean aggregates
    from the authoritative fixed-range post-model GLCM in geo_postprocess.py.
  - This script does NOT train any classifier.

Usage:
    python b2_export.py --meta outputs/012_b2/00012_spill_meta.json --out b2_features.csv
    python b2_export.py --meta outputs/*/00012_spill_meta.json --out b2_features.csv
"""

import argparse
import csv
import glob
import json
from pathlib import Path

META_COLUMNS = [
    "candidate_id", "scene_id", "source_id", "label", "label_name",
    "category", "scene_datetime", "centroid_lat", "centroid_lon",
]

FEATURE_COLUMNS = [
    "mean_backscatter", "std_backscatter",
    "glcm_contrast", "glcm_homogeneity", "glcm_energy", "glcm_correlation",
    "area_km2", "perimeter_m", "elongation", "boundary_irregularity",
    "edge_sharpness", "wind_speed_kmh", "distance_to_nearest_vessel_km",
    "persistence_count",
]

CSV_HEADER = META_COLUMNS + FEATURE_COLUMNS


def _num(value):
    """Format a numeric value; None/non-finite -> empty string (never 0)."""
    if value is None:
        return ""
    try:
        import math
        f = float(value)
        if not math.isfinite(f):
            return ""
        if isinstance(value, bool):
            return ""
        if isinstance(value, int) or (isinstance(f, float) and f.is_integer()):
            return str(int(f))
        return repr(f)
    except (TypeError, ValueError):
        return ""


def candidate_to_row(meta: dict, candidate: dict) -> dict:
    """Map one spill_meta.json candidate to the exact B2 CSV schema."""
    glcm = (candidate.get("texture") or {}).get("glcm") or {}
    centroid = candidate.get("centroid_lonlat") or [None, None]
    area_m2 = candidate.get("area_m2")
    row = {
        "candidate_id": candidate.get("candidate_id", ""),
        "scene_id": meta.get("stem", ""),
        "source_id": meta.get("source_id", meta.get("stem", "")),
        "label": "",
        "label_name": "",
        "category": "",
        "scene_datetime": meta.get("scene_datetime", "") or "",
        "centroid_lat": _num(centroid[1] if len(centroid) > 1 else None),
        "centroid_lon": _num(centroid[0] if len(centroid) > 0 else None),
        "mean_backscatter": _num(candidate.get("mean_backscatter")),
        "std_backscatter": _num(candidate.get("std_backscatter")),
        "glcm_contrast": _num(glcm.get("contrast_mean")),
        "glcm_homogeneity": _num(glcm.get("homogeneity_mean")),
        "glcm_energy": _num(glcm.get("energy_mean")),
        "glcm_correlation": _num(glcm.get("correlation_mean")),
        "area_km2": _num(area_m2 / 1e6) if area_m2 is not None else "",
        "perimeter_m": _num(candidate.get("perimeter_m")),
        "elongation": _num(candidate.get("elongation")),
        "boundary_irregularity": _num(candidate.get("boundary_irregularity")),
        "edge_sharpness": _num(candidate.get("edge_sharpness")),
        "wind_speed_kmh": _num(candidate.get("wind_speed_kmh")),
        "distance_to_nearest_vessel_km": _num(candidate.get("distance_to_nearest_vessel_km")),
        "persistence_count": _num(candidate.get("persistence_count")),
    }
    return row


def export_csv(meta_paths, out_path) -> int:
    """Export all candidates from the given meta files. Returns row count."""
    rows = []
    for pattern in meta_paths:
        for path in sorted(glob.glob(pattern, recursive=True)):
            with open(path, encoding="utf-8") as f:
                meta = json.load(f)
            for candidate in meta.get("candidates", []):
                rows.append(candidate_to_row(meta, candidate))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[b2_export] wrote {len(rows)} candidate row(s) from "
          f"{len(meta_paths)} pattern(s) -> {out_path}")
    return len(rows)


def parse_args():
    p = argparse.ArgumentParser(description="Export B2 candidate feature CSV from spill_meta.json files")
    p.add_argument("--meta", nargs="+", required=True,
                   help="spill_meta.json path(s) or glob pattern(s)")
    p.add_argument("--out", required=True, help="Output CSV path")
    return p.parse_args()


def main():
    args = parse_args()
    export_csv(args.meta, args.out)


if __name__ == "__main__":
    main()
