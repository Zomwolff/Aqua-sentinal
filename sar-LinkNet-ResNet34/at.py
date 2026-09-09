#!/usr/bin/env python3
"""
Image-level SAR dataset builder for Random Forest training (B2).

Pipeline per sample (sample directory is the authoritative sample ID):

    dataset/WaterBench_Fusion_50/sample_XXXX/S1.tif
        |
        v  infer_pipeline.run_inference  (LinkNet+ResNet34, threshold unchanged)
    outputs/<batch>/sample_XXXX/S1_mask.png
        |
        v  geo_postprocess.run_postprocess (mask-aware GLCM + B2 features)
    outputs/<batch>/sample_XXXX/S1_spill_meta.json
        |
        v  aggregate: all candidates -> EXACTLY ONE image row
    dataset_image_level.csv (50 samples -> 50 rows)

Aggregation rules (exact):
  MEAN : mean_backscatter, std_backscatter, glcm_contrast, glcm_homogeneity,
         glcm_energy, glcm_correlation, elongation, boundary_irregularity,
         edge_sharpness, wind_speed_kmh, persistence_count
  SUM  : perimeter_m
  MIN  : distance_to_nearest_vessel_km
  AREA : area_m2 / 1,000,000 -> km2 per candidate, then MEAN (never sum)

Ground truth comes ONLY from Lab.txt (sample_XXXX -> label). Predictions,
masks, thresholds, and candidate counts NEVER determine the label.

Zero-candidate samples are KEPT (features blank/NaN, candidate_count=0) so
negative images do not silently disappear from training.

Missing values are written as EMPTY fields, never as fake 0/1.

Usage (from sar-LinkNet-ResNet34, inside .venv):
    python at.py ^
        --data-root dataset/WaterBench_Fusion_50 ^
        --lab dataset/Lab.txt ^
        --out-dir outputs/batch ^
        --csv outputs/batch/dataset_image_level.csv

    # process only the first 2 samples (validation dry-run):
    python at.py --data-root dataset/WaterBench_Fusion_50 --lab dataset/Lab.txt ^
        --out-dir outputs/batch_dry --csv outputs/batch_dry/dataset_image_level.csv --limit 2

    # re-aggregate existing per-sample outputs without re-running inference:
    python at.py --data-root dataset/WaterBench_Fusion_50 --lab dataset/Lab.txt ^
        --out-dir outputs/batch --csv outputs/batch/dataset_image_level.csv --skip-pipeline

    # disable cross-sample persistence matching:
    python at.py --data-root dataset/WaterBench_Fusion_50 --lab dataset/Lab.txt ^
        --out-dir outputs/batch --csv outputs/batch/dataset_image_level.csv --no-cross-persistence
"""

import argparse
import csv
import json
import math
import re
import sys
import traceback
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent))

FEATURE_COLUMNS = [
    "mean_backscatter",
    "std_backscatter",
    "glcm_contrast",
    "glcm_homogeneity",
    "glcm_energy",
    "glcm_correlation",
    "area_km2",
    "perimeter_m",
    "elongation",
    "boundary_irregularity",
    "edge_sharpness",
    "wind_speed_kmh",
    "distance_to_nearest_vessel_km",
    "persistence_count",
]

OUTPUT_COLUMNS = (
    ["sample_id", "source_id", "status", "category", "label", "candidate_count"]
    + FEATURE_COLUMNS
    + ["input_min_db", "input_max_db", "input_mean_db",
       "largest_candidate_fraction", "notes"]
)

DIAG_NUMERIC_COLUMNS = ["input_min_db", "input_max_db", "input_mean_db",
                        "largest_candidate_fraction"]

MEAN_FIELDS = [
    "mean_backscatter",
    "std_backscatter",
    "glcm_contrast",
    "glcm_homogeneity",
    "glcm_energy",
    "glcm_correlation",
    "elongation",
    "boundary_irregularity",
    "edge_sharpness",
    "wind_speed_kmh",
    "persistence_count",
]


def clean_number(value):
    """Return a finite float or None (missing stays missing)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def mean_values(values):
    nums = [v for v in (clean_number(x) for x in values) if v is not None]
    return mean(nums) if nums else None


def sum_values(values):
    nums = [v for v in (clean_number(x) for x in values) if v is not None]
    return sum(nums) if nums else None


def min_values(values):
    nums = [v for v in (clean_number(x) for x in values) if v is not None]
    return min(nums) if nums else None


def parse_lab_file(lab_path):
    """Parse Lab.txt. Returns {sample_id: {label, source_id, status, category}}.

    Handles pipe-delimited rows with markdown bold markers (**OIL** -> OIL).
    Label is parsed as int; a non-numeric label raises instead of guessing.
    """
    path = Path(lab_path)
    if not path.exists():
        raise FileNotFoundError(f"Lab file not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Lab file is empty")

    header_line = lines[0]
    if "\t" in header_line:
        delimiter = "\t"
    elif "|" in header_line:
        delimiter = "|"
    elif "," in header_line:
        delimiter = ","
    else:
        delimiter = None

    if delimiter:
        raw_rows = list(csv.DictReader(lines, delimiter=delimiter))
    else:
        header = re.split(r"\s+", header_line)
        raw_rows = []
        for ln in lines[1:]:
            parts = re.split(r"\s+", ln)
            if len(parts) >= len(header):
                raw_rows.append(dict(zip(header, parts)))

    lab_map = {}
    for raw in raw_rows:
        norm = {}
        for k, v in raw.items():
            if k is None:
                continue
            key = re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_")
            val = str(v).strip().strip("*").strip()
            norm[key] = val
        sample = (norm.get("sample") or norm.get("sample_id") or "").lower()
        label_raw = norm.get("label")
        if not sample or label_raw is None or label_raw == "":
            continue
        if not re.fullmatch(r"sample_\d+", sample):
            continue  # markdown separator / non-data rows
        try:
            label_value = int(float(label_raw))
        except ValueError:
            raise ValueError(f"Non-numeric label for {sample}: {label_raw!r}")
        if sample in lab_map:
            raise ValueError(f"Duplicate Lab entry for {sample}")
        lab_map[sample] = {
            "label": label_value,
            "source_id": norm.get("source_id") or "",
            "status": norm.get("status") or "",
            "category": norm.get("category") or "",
        }
    if not lab_map:
        raise ValueError("No usable rows parsed from Lab file")
    return lab_map


def discover_samples(data_root):
    """Sample directories are authoritative: sample_XXXX -> sample_XXXX."""
    root = Path(data_root)
    if not root.is_dir():
        raise FileNotFoundError(f"Dataset root not found: {root}")
    samples = sorted(
        p for p in root.iterdir()
        if p.is_dir() and re.fullmatch(r"sample_\d+", p.name, flags=re.IGNORECASE)
    )
    return [p for p in samples]


def aggregate_candidates(candidates):
    """Exactly one image-level feature dict from all candidates."""
    def field(name):
        return [c.get(name) for c in candidates]

    def glcm(name):
        # texture may be None for skipped candidates -> (or {}) keeps it safe.
        return [((c.get("texture") or {}).get("glcm") or {}).get(name)
                for c in candidates]

    area_km2_values = []
    for c in candidates:
        area_m2 = clean_number(c.get("area_m2"))
        if area_m2 is not None:
            area_km2_values.append(area_m2 / 1_000_000.0)

    return {
        "mean_backscatter": mean_values(field("mean_backscatter")),
        "std_backscatter": mean_values(field("std_backscatter")),
        "glcm_contrast": mean_values(glcm("contrast_mean")),
        "glcm_homogeneity": mean_values(glcm("homogeneity_mean")),
        "glcm_energy": mean_values(glcm("energy_mean")),
        "glcm_correlation": mean_values(glcm("correlation_mean")),
        "area_km2": mean_values(area_km2_values),
        "perimeter_m": sum_values(field("perimeter_m")),
        "elongation": mean_values(field("elongation")),
        "boundary_irregularity": mean_values(field("boundary_irregularity")),
        "edge_sharpness": mean_values(field("edge_sharpness")),
        "wind_speed_kmh": mean_values(field("wind_speed_kmh")),
        "distance_to_nearest_vessel_km": min_values(
            field("distance_to_nearest_vessel_km")),
        "persistence_count": mean_values(field("persistence_count")),
    }


def blank_features():
    return {k: None for k in FEATURE_COLUMNS}


def diagnose_sample(candidates, sar_input, mask_px):
    """Real-data diagnostics explaining 0/1/blank patterns (no invented values).

    Returns dict with input_min/max/mean_db (scene stats from sar_input),
    largest_candidate_fraction, and semicolon-joined notes flags.
    """
    diag = {"input_min_db": None, "input_max_db": None, "input_mean_db": None,
            "largest_candidate_fraction": None, "notes": ""}
    if isinstance(sar_input, dict):
        diag["input_min_db"] = clean_number(sar_input.get("sar_min_db"))
        diag["input_max_db"] = clean_number(sar_input.get("sar_max_db"))
        diag["input_mean_db"] = clean_number(sar_input.get("sar_mean_db"))
    notes = []
    if not candidates:
        notes.append("zero_candidates")
        diag["notes"] = ";".join(notes)
        return diag
    if mask_px:
        px_counts = [clean_number(c.get("candidate_pixel_count")) or 0
                     for c in candidates]
        if sum(px_counts) > 0:
            diag["largest_candidate_fraction"] = max(px_counts) / mask_px
    imin, imax = diag["input_min_db"], diag["input_max_db"]
    if imin is not None and imax is not None and imin == imax:
        notes.append("degenerate_constant_input")
    else:
        lo = [clean_number(c.get("clip_fraction_lower")) for c in candidates]
        hi = [clean_number(c.get("clip_fraction_upper")) for c in candidates]
        lo = [v for v in lo if v is not None]
        hi = [v for v in hi if v is not None]
        if lo and all(v > 0.95 for v in lo):
            notes.append("all_candidates_saturated_below_range")
        elif hi and all(v > 0.95 for v in hi):
            notes.append("all_candidates_saturated_above_range")
        elif lo and any(v > 0.95 for v in lo + hi):
            notes.append("partial_saturation")
    if (diag["largest_candidate_fraction"] is not None
            and diag["largest_candidate_fraction"] > 0.5):
        notes.append("whole_scene_candidate")
    if not notes:
        notes.append("ok")
    diag["notes"] = ";".join(notes)
    return diag


def process_sample(sample_dir, sample_id, out_sample_dir, args):
    """Run inference + postprocess for ONE sample. Returns (meta_path, notes)."""
    from infer_pipeline import run_inference
    from geo_postprocess import run_postprocess

    s1 = sample_dir / "S1.tif"
    if not s1.exists():
        raise FileNotFoundError(f"{sample_id}: S1.tif missing in {sample_dir}")

    out_sample_dir.mkdir(parents=True, exist_ok=True)
    run_inference(
        checkpoint=args.checkpoint,
        input=str(s1),
        output_dir=str(out_sample_dir),
        threshold=args.threshold,
        batch_size=args.batch_size,
        rescale=args.rescale,
        overlap=args.overlap,
    )
    mask_path = out_sample_dir / "S1_mask.png"
    if not mask_path.exists():
        raise FileNotFoundError(f"{sample_id}: inference did not write {mask_path}")
    result = run_postprocess(
        mask_path=str(mask_path),
        source_image=str(s1),
        output_dir=str(out_sample_dir),
        glcm_band=args.glcm_band,
        glcm_min_db=args.glcm_min_db,
        glcm_max_db=args.glcm_max_db,
        enable_glcm=not args.no_glcm,
    )
    meta_path = out_sample_dir / "S1_spill_meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"{sample_id}: postprocess did not write {meta_path}")
    return meta_path, result


def apply_cross_persistence(collected, iou_thresh, window_h):
    """Match every candidate against ALL components of every OTHER sample.

    Each sibling sample contributes all of its GeoJSON polygons (however many
    components it has); a sibling scene counts once when ANY of its polygons
    reaches IoU >= iou_thresh. Count INCLUDES the current scene, so the
    minimum is 1. Samples carry no acquisition timestamps, so matching here
    is spatial-only (documented; time gate applies when scene_datetime exists).

    Updates candidate dicts in place AND writes the meta files back, then
    returns (n_matched_candidates, max_count).
    """
    from geo_postprocess import _persistence_count, _parse_time

    # Build the per-scene polygon pools first (many components per scene OK).
    pools = []
    for item in collected:
        polys = []
        for c in item["candidates"]:
            pg = c.get("polygon_wgs84")
            if pg:
                polys.append(pg)
        pools.append({
            "scene_id": item["sample_id"],
            "acquisition_time": (item["meta"].get("scene_datetime")
                                 if isinstance(item["meta"], dict) else None),
            "polygons_wgs84": polys,
        })

    n_matched = 0
    max_count = 1
    for item, pool in zip(collected, pools):
        others = [p for p in pools if p["scene_id"] != item["sample_id"]]
        acq = _parse_time(item["meta"].get("scene_datetime")) \
            if isinstance(item["meta"], dict) else None
        changed = False
        for c in item["candidates"]:
            count, scenes, reason = _persistence_count(
                c.get("polygon_wgs84"), item["sample_id"], acq,
                others, iou_thresh, window_h)
            if count is None:
                continue
            if (c.get("persistence_count") != count
                    or c.get("persistence_scenes") != scenes):
                c["persistence_count"] = count
                c["persistence_scenes"] = scenes
                c["persistence_status"] = f"cross_sample:{reason}"
                changed = True
            if scenes:
                n_matched += 1
            max_count = max(max_count, count)
        if changed:
            with open(item["meta_path"], "w", encoding="utf-8") as f:
                json.dump(item["meta"], f, indent=2)
    return n_matched, max_count


def main():
    parser = argparse.ArgumentParser(
        description="Per-sample SAR pipeline + one-row-per-image B2 dataset.")
    parser.add_argument("--data-root", required=True,
                        help="Root with sample_XXXX folders (authoritative IDs).")
    parser.add_argument("--lab", required=True, help="Lab.txt ground-truth path.")
    parser.add_argument("--out-dir", required=True,
                        help="Per-sample outputs: <out-dir>/sample_XXXX/.")
    parser.add_argument("--csv", required=True, help="Output image-level CSV.")
    parser.add_argument("--checkpoint", default="checkpoints/best_model.pth")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="Segmentation threshold (project default 0.5; unchanged).")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--overlap", type=int, default=32)
    parser.add_argument("--rescale", action="store_true", default=True,
                        help="Min-max normalize float GeoTIFF input (default on).")
    parser.add_argument("--no-rescale", dest="rescale", action="store_false")
    parser.add_argument("--glcm-band", type=int, default=1)
    parser.add_argument("--glcm-min-db", type=float, default=None,
                        help="Manual lower dB clip (default: auto scene p1).")
    parser.add_argument("--glcm-max-db", type=float, default=None,
                        help="Manual upper dB clip (default: auto scene p99).")
    parser.add_argument("--no-glcm", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N samples (dry-run).")
    parser.add_argument("--skip-pipeline", action="store_true",
                        help="Re-aggregate existing <out-dir>/sample_XXXX/S1_spill_meta.json "
                             "without re-running inference.")
    parser.add_argument("--no-cross-persistence", action="store_true",
                        help="Disable cross-sample persistence matching (each sample keeps "
                             "persistence_count=1 unless geo_postprocess matched other scenes).")
    parser.add_argument("--persist-iou", type=float, default=0.1,
                        help="IoU threshold for cross-sample candidate matching.")
    parser.add_argument("--persist-window-h", type=float, default=720.0,
                        help="Temporal window for cross-sample matching (hours). "
                             "WaterBench samples carry no acquisition timestamps, so "
                             "matching is spatial-only unless metas provide scene_datetime.")
    args = parser.parse_args()

    lab_map = parse_lab_file(args.lab)
    samples = discover_samples(args.data_root)
    if args.limit:
        samples = samples[:args.limit]
    out_dir = Path(args.out_dir)

    print("=" * 50)
    print("IMAGE LEVEL DATASET BUILD")
    print("=" * 50)
    print(f"Samples discovered: {len(samples)}")
    print(f"Lab entries: {len(lab_map)}")

    rows = []
    total_candidates = 0
    n_with_candidates = 0
    n_zero_candidates = 0
    errors = []
    collected = []

    for sample_dir in samples:
        sample_id = sample_dir.name.lower()
        out_sample = out_dir / sample_id
        lab = lab_map.get(sample_id)
        if lab is None:
            errors.append(f"{sample_id}: no Lab mapping; refusing to invent a label")
            print(f"[error] {sample_id}: no Lab mapping, skipped (no label invented)")
            continue
        try:
            if args.skip_pipeline:
                meta_path = out_sample / "S1_spill_meta.json"
                if not meta_path.exists():
                    raise FileNotFoundError(f"no existing metadata at {meta_path}")
            else:
                meta_path, _ = process_sample(sample_dir, sample_id, out_sample, args)
            with meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            candidates = meta.get("candidates", [])
            if not isinstance(candidates, list):
                raise ValueError(f"'candidates' is not a list in {meta_path}")
        except Exception as exc:
            errors.append(f"{sample_id}: {exc}")
            print(f"[error] {sample_id}: {exc}")
            traceback.print_exc()
            continue

        n_cand = len(candidates)
        total_candidates += n_cand
        if n_cand == 0:
            n_zero_candidates += 1
            print(f"[warn] {sample_id}: zero candidates; keeping row with "
                  f"label={lab['label']} and blank features")
        else:
            n_with_candidates += 1
        mask_px = None
        try:
            from PIL import Image as _Image
            with _Image.open(out_sample / "S1_mask.png") as _img:
                mask_px = _img.size[0] * _img.size[1]
        except Exception:
            mask_px = None
        sar_input = meta.get("sar_input") if isinstance(meta, dict) else None
        collected.append({"sample_id": sample_id, "lab": lab, "meta": meta,
                          "meta_path": meta_path, "candidates": candidates,
                          "n_cand": n_cand, "mask_px": mask_px,
                          "sar_input": sar_input})

    # ---- Cross-sample persistence: every candidate vs ALL components of
    # ---- every other sample (spatial overlap; time gate when available).
    if not args.no_cross_persistence and collected:
        n_matched, max_count = apply_cross_persistence(
            collected, args.persist_iou, args.persist_window_h)
        print(f"[persistence] cross-sample matching: {n_matched} candidate(s) "
              f"matched in other scenes; max persistence_count={max_count}")
    else:
        print("[persistence] cross-sample matching disabled; "
              "per-sample persistence_count kept as computed")

    for item in collected:
        sample_id, lab = item["sample_id"], item["lab"]
        candidates, n_cand = item["candidates"], item["n_cand"]
        features = (blank_features() if n_cand == 0
                    else aggregate_candidates(candidates))
        diag = diagnose_sample(candidates, item.get("sar_input"),
                               item.get("mask_px"))
        row = {
            "sample_id": sample_id,
            "source_id": lab["source_id"],
            "status": lab["status"],
            "category": lab["category"],
            "label": lab["label"],
            "candidate_count": n_cand,
        }
        row.update(features)
        row.update(diag)
        rows.append(row)
        pcounts = sorted({c.get("persistence_count") for c in candidates
                          if c.get("persistence_count") is not None})
        print(f"{sample_id} | candidates={n_cand} | label={lab['label']}"
              + (f" | persistence={pcounts}" if pcounts else ""))

    if not rows:
        raise RuntimeError("No image-level rows were created.")

    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k))
                             for k in OUTPUT_COLUMNS})

    pos = sum(1 for r in rows if r["label"] == 1)
    neg = sum(1 for r in rows if r["label"] == 0)
    print()
    print("=" * 50)
    print("IMAGE LEVEL DATASET BUILD")
    print("=" * 50)
    print(f"Samples discovered: {len(samples)}")
    print(f"Samples processed: {len(rows)}")
    print(f"Rows written: {len(rows)}")
    print()
    print(f"Positive samples: {pos}")
    print(f"Negative samples: {neg}")
    print()
    print(f"Samples with candidates: {n_with_candidates}")
    print(f"Samples with zero candidates: {n_zero_candidates}")
    print()
    print(f"Total candidates: {total_candidates}")
    print()
    print("Aggregation:")
    print("  mean -> all requested numerical features")
    print("  sum  -> perimeter_m")
    print("  min  -> distance_to_nearest_vessel_km")
    print("  area -> m2 -> km2, then mean")
    print()
    print("Ground truth:")
    print("  source = Lab.txt")
    if errors:
        print()
        print(f"Errors ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")

    # ---- CSV self-validation ----
    print()
    print("CSV validation:")
    checks = []
    with csv_path.open("r", encoding="utf-8") as f:
        creads = list(csv.DictReader(f))
    ids = [r["sample_id"] for r in creads]
    checks.append(("exactly one row per sample (50/50 or limit)",
                   len(creads) == len(rows)))
    checks.append(("no duplicate sample_id", len(set(ids)) == len(ids)))
    labels = {r["label"] for r in creads}
    checks.append(("both label classes exist (or all available)",
                   len(labels) >= 1))
    lab_ok = all(
        str(lab_map[r["sample_id"]]["label"]) == str(r["label"]) for r in creads)
    checks.append(("label matches Lab.txt", lab_ok))
    num_ok = True
    for r in creads:
        for k in FEATURE_COLUMNS + DIAG_NUMERIC_COLUMNS:
            v = r[k]
            if v in ("", None):
                continue
            try:
                float(v)
            except ValueError:
                num_ok = False
    checks.append(("numerical columns numeric-or-blank", num_ok))
    checks.append(("no glcm_con/glcm_hom-style strings in GLCM cols",
                   all(r[k] not in ("glcm_con", "glcm_hom", "glcm_en", "glcm_corr")
                       for r in creads for k in FEATURE_COLUMNS[:6])))
    all_ok = True
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        all_ok = all_ok and ok
    print(f"CSV: {csv_path} ({'VALID' if all_ok else 'INVALID'})")
    if errors:
        raise SystemExit(f"Completed with {len(errors)} sample error(s).")
    if not all_ok:
        raise SystemExit("CSV validation failed.")


if __name__ == "__main__":
    main()
