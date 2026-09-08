#!/usr/bin/env python3
"""B2 Look-Alike Classifier inference (POST-SEGMENTATION).

Pipeline position:

    Sentinel-1 SAR -> LinkNet+ResNet34 -> mask -> candidate polygons
    -> 14 B2 features (geo_postprocess.py) -> b2_random_forest.joblib
    -> OIL / LOOK_ALIKE + Random Forest predicted probabilities
    -> (later) C1 Multi-Modal Evidence Fusion

What B2 is: a look-alike classifier over SAR backscatter, GLCM texture,
shape, edge, wind, AIS proximity, and temporal persistence features.
What B2 is NOT: final incident confidence (that belongs to C1), nor proof
of oil. ``oil_probability`` is the Random Forest's predicted probability
for the OIL class -- a model confidence, NOT a physical certainty.

Reads spill_meta.json candidates directly (no manual CSV needed), writes an
enriched JSON + flat CSV + B2-enriched GeoJSON/SHP beside the input when
source vector files exist. Original metadata is preserved; B2 results are
added under each candidate's ``b2`` key.

Usage (from sar-LinkNet-ResNet34, inside .venv):
    python b2_lookalike.py --input outputs/batch/sample_0001/S1_spill_meta.json
    python b2_lookalike.py --input outputs/batch/sample_0001/S1_spill_meta.json ^
        --output outputs/b2_predictions.json --oil-threshold 0.5
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CLASS_LABELS = {1: "OIL", 0: "LOOK_ALIKE"}

CSV_COLUMNS = [
    "candidate_id", "scene_id", "acquisition_time", "centroid_lat",
    "centroid_lon",
    "mean_backscatter", "std_backscatter", "glcm_contrast",
    "glcm_homogeneity", "glcm_energy", "glcm_correlation", "area_km2",
    "perimeter_m", "elongation", "boundary_irregularity", "edge_sharpness",
    "wind_speed_kmh", "distance_to_nearest_vessel_km", "persistence_count",
    "oil_probability", "lookalike_probability", "classification_code",
    "classification_label", "classification_status",
]

B2_SHP_FIELDS = {"b2_label": 10, "b2_code": 10, "b2_oil_p": 10,
                 "b2_lk_p": 10, "b2_stat": 10}


def load_model(model_path, features_path):
    """Load joblib + authoritative feature order; fail loudly on problems."""
    import joblib
    model_path, features_path = Path(model_path), Path(features_path)
    if not model_path.exists():
        raise FileNotFoundError(f"model file missing: {model_path}")
    if not features_path.exists():
        raise FileNotFoundError(f"feature list missing: {features_path}")
    try:
        model = joblib.load(model_path)
    except Exception as e:
        raise ValueError(f"invalid/unreadable model {model_path}: {e}") from e
    try:
        with open(features_path, encoding="utf-8") as f:
            features = json.load(f)
    except Exception as e:
        raise ValueError(f"malformed feature JSON {features_path}: {e}") from e
    if not isinstance(features, list) or len(features) != 14:
        raise ValueError(f"feature list must hold exactly 14 names, got: {features}")
    if not hasattr(model, "predict"):
        raise ValueError("loaded object has no predict(); not a classifier")
    steps = getattr(model, "named_steps", {}) or {}
    estimator = steps.get("model", model)
    _classes = getattr(estimator, "classes_", None)
    classes = list(_classes) if _classes is not None else []
    if 0 not in classes or 1 not in classes:
        raise ValueError(f"model classes must contain 0 and 1, got: {classes}")
    has_imputer = "imputer" in getattr(model, "named_steps", {})
    return model, features, has_imputer


def _num(value):
    """Finite float or None (missing stays missing; inf -> None)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def extract_b2_features(candidate, feature_order):
    """Return the 14 B2 features in exact model order (None where missing).

    Mapping follows the real spill_meta.json schema:
      texture.glcm.{contrast,homogeneity,energy,correlation}_mean -> glcm_*
      area_m2 / 1e6 -> area_km2
      all other features read directly off the candidate.
    """
    glcm = ((candidate.get("texture") or {}).get("glcm") or {})
    area_m2 = _num(candidate.get("area_m2"))
    raw = {
        "mean_backscatter": _num(candidate.get("mean_backscatter")),
        "std_backscatter": _num(candidate.get("std_backscatter")),
        "glcm_contrast": _num(glcm.get("contrast_mean")),
        "glcm_homogeneity": _num(glcm.get("homogeneity_mean")),
        "glcm_energy": _num(glcm.get("energy_mean")),
        "glcm_correlation": _num(glcm.get("correlation_mean")),
        "area_km2": (area_m2 / 1_000_000.0) if area_m2 is not None else None,
        "perimeter_m": _num(candidate.get("perimeter_m")),
        "elongation": _num(candidate.get("elongation")),
        "boundary_irregularity": _num(candidate.get("boundary_irregularity")),
        "edge_sharpness": _num(candidate.get("edge_sharpness")),
        "wind_speed_kmh": _num(candidate.get("wind_speed_kmh")),
        "distance_to_nearest_vessel_km": _num(
            candidate.get("distance_to_nearest_vessel_km")),
        "persistence_count": _num(candidate.get("persistence_count")),
    }
    missing = [k for k in feature_order if raw.get(k) is None]
    return [raw[k] for k in feature_order], missing, raw


def classify_candidates(meta, model, feature_order, has_imputer, oil_threshold):
    """Classify every candidate; returns (rows, n_insufficient)."""
    import numpy as np
    steps = getattr(model, "named_steps", {}) or {}
    estimator = steps.get("model", model)
    classes = list(getattr(estimator, "classes_", []))
    oil_idx = classes.index(1)
    lk_idx = classes.index(0)
    rows, n_insufficient = [], 0
    for cand in meta.get("candidates", []):
        vec, missing, raw = extract_b2_features(cand, feature_order)
        b2 = {"missing_features": missing}
        if missing and not has_imputer:
            b2.update({"classification_label": None,
                       "classification_code": None,
                       "oil_probability": None,
                       "lookalike_probability": None,
                       "classification_status": "insufficient_features"})
            n_insufficient += 1
        else:
            X = np.array([[v if v is not None else np.nan for v in vec]],
                         dtype=float)
            proba = model.predict_proba(X)[0]
            oil_p, lk_p = float(proba[oil_idx]), float(proba[lk_idx])
            code = 1 if oil_p >= float(oil_threshold) else 0
            b2.update({
                "classification_label": CLASS_LABELS[code],
                "classification_code": code,
                "oil_probability": oil_p,
                "lookalike_probability": lk_p,
                "classification_status": "predicted",
            })
        cand["b2"] = b2
        centroid = cand.get("centroid_lonlat") or [None, None]
        rows.append({
            "candidate_id": cand.get("candidate_id"),
            "scene_id": meta.get("stem", ""),
            "acquisition_time": meta.get("scene_datetime", "") or "",
            "centroid_lat": centroid[1] if len(centroid) > 1 else None,
            "centroid_lon": centroid[0] if len(centroid) > 0 else None,
            **raw,
            "oil_probability": b2["oil_probability"],
            "lookalike_probability": b2["lookalike_probability"],
            "classification_code": b2["classification_code"],
            "classification_label": b2["classification_label"],
            "classification_status": b2["classification_status"],
        })
    return rows, n_insufficient


def write_enriched_vectors(meta, rows, out_base):
    """B2-enriched GeoJSON/SHP next to the source vectors (matched by
    candidate_id; geometry/CRS untouched; SHP names shortened to <=10)."""
    made = []
    src_geo = (meta.get("geojson") or "")
    if not src_geo:
        return made
    try:
        import geopandas as gpd
        gdf = gpd.read_file(src_geo)
    except Exception as e:
        print(f"[b2] vector enrichment skipped (cannot read {src_geo}): {e}")
        return made
    by_id = {r["candidate_id"]: r for r in rows}
    ids = gdf["candidate_id"] if "candidate_id" in gdf.columns else gdf.get("id")
    for full, short in [("classification_label", "b2_label"),
                        ("classification_code", "b2_code"),
                        ("oil_probability", "b2_oil_p"),
                        ("lookalike_probability", "b2_lk_p"),
                        ("classification_status", "b2_stat")]:
        gdf[full] = [by_id.get(i, {}).get(full) for i in ids]
        gdf[short] = gdf[full]
    gj_path = out_base.parent / (out_base.stem + "_candidates.geojson")
    gdf.drop(columns=[c for c in ["b2_label", "b2_code", "b2_oil_p",
                                  "b2_lk_p", "b2_stat"] if c in gdf.columns]
             ).to_file(gj_path, driver="GeoJSON")
    made.append(str(gj_path))
    try:
        shp_path = out_base.parent / (out_base.stem + "_candidates.shp")
        keep = [c for c in gdf.columns
                if c in ("geometry", "b2_label", "b2_code",
                         "b2_oil_p", "b2_lk_p", "b2_stat")]
        shp = gdf[keep].copy()
        if "candidate_id" in gdf.columns:
            shp["cand_id"] = gdf["candidate_id"].astype(str).str.slice(0, 10)
            cols = ["cand_id"] + [c for c in keep if c != "geometry"] + ["geometry"]
            shp = shp[cols]
        shp.to_file(shp_path, driver="ESRI Shapefile")
        made.append(str(shp_path))
    except Exception as e:
        print(f"[b2] shapefile write skipped: {e}")
    return made


def main():
    ap = argparse.ArgumentParser(description="B2 look-alike inference (post-segmentation).")
    ap.add_argument("--input", required=True, help="spill_meta.json path")
    ap.add_argument("--output", default=None, help="enriched JSON path")
    ap.add_argument("--csv", default=None, help="flat CSV path")
    ap.add_argument("--model", default="models/b2_random_forest.joblib")
    ap.add_argument("--features", default="models/b2_feature_columns.json")
    ap.add_argument("--oil-threshold", type=float, default=0.5,
                    help="B2 decision threshold on oil_probability (NOT the SAR "
                         "segmentation threshold; default 0.5 = model.predict).")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        raise FileNotFoundError(f"input JSON missing: {in_path}")
    try:
        with open(in_path, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as e:
        raise ValueError(f"malformed input JSON {in_path}: {e}") from e
    candidates = meta.get("candidates", [])
    if not isinstance(candidates, list) or not candidates:
        raise ValueError(f"no candidates in {in_path}; nothing to classify")

    model, feature_order, has_imputer = load_model(args.model, args.features)
    print(f"Model: {args.model} "
          f"({'Pipeline+imputer' if has_imputer else 'bare classifier'})")
    print(f"Features: {len(feature_order)} "
          f"({', '.join(feature_order[:3])} ... {feature_order[-1]})")
    print(f"Candidates: {len(candidates)}")

    rows, n_insufficient = classify_candidates(
        meta, model, feature_order, has_imputer, args.oil_threshold)
    n_oil = sum(1 for r in rows if r["classification_label"] == "OIL")
    n_lk = sum(1 for r in rows if r["classification_label"] == "LOOK_ALIKE")
    n_pred = len(rows) - n_insufficient
    print(f"{'candidate_id':<14}{'oil_prob':>10}  {'label':<10}  status")
    for r in rows:
        p = ("---" if r["oil_probability"] is None
             else f"{r['oil_probability']:.2f}")
        lab = r["classification_label"] or "---"
        print(f"{str(r['candidate_id']):<14}{p:>10}  {lab:<10}  "
              f"{r['classification_status']}")

    meta["b2_summary"] = {
        "total_candidates": len(rows),
        "predicted_candidates": n_pred,
        "insufficient_feature_candidates": n_insufficient,
        "oil_count": n_oil,
        "lookalike_count": n_lk,
        "oil_fraction": (n_oil / n_pred) if n_pred else None,
        "model_path": str(args.model),
        "feature_columns": feature_order,
        "threshold": float(args.oil_threshold),
    }
    out_base = Path(args.output) if args.output else in_path.parent / "b2_predictions"
    out_json = out_base.with_suffix(".json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    csv_path = Path(args.csv) if args.csv else out_base.with_suffix(".csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k))
                        for k in CSV_COLUMNS})
    made = write_enriched_vectors(meta, rows, out_json.with_suffix(""))
    print(f"[b2] oil={n_oil} lookalike={n_lk} insufficient={n_insufficient}")
    print(f"[b2] wrote {out_json}, {csv_path}" + (f", {', '.join(made)}" if made else ""))

    # PART 17: reload compatibility.
    import joblib as _jl
    reloaded = _jl.load(args.model)
    import numpy as _np
    Xr = _np.array([[v if v is not None else float("nan")
                     for v in extract_b2_features(c, feature_order)[0]]
                    for c in meta.get("candidates", [])], dtype=float)
    assert (reloaded.predict(Xr) == model.predict(Xr)).all()
    print("[b2] reload check: predictions identical")


if __name__ == "__main__":
    main()
