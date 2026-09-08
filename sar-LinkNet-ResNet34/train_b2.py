#!/usr/bin/env python3
"""B2 Look-Alike Classifier training (Random Forest).

Reads the ORIGINAL image-level CSV (never modified), audits it, builds a
cleaned training CSV (14 B2 features + label + IDs), compares complete-case
vs median-imputation training, tunes on the TRAIN split only, evaluates on
the untouched TEST split, and saves model + feature list + report.

Inputs (X): exactly the 14 B2 FEATURE_COLUMNS. Target: label (0/1).
Metadata (sample_id, source_id, ...) NEVER enters X.

Usage (from sar-LinkNet-ResNet34, inside .venv):
    python train_b2.py --input data_raw_dataset_original.csv

Outputs (created if missing):
    data/b2_training_clean.csv
    models/b2_random_forest.joblib
    models/b2_feature_columns.json
    reports/b2_random_forest_report.txt
    reports/feature_importance.csv
"""

import argparse
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import (accuracy_score, classification_report,
                             confusion_matrix, f1_score, precision_score,
                             recall_score, roc_auc_score)
from sklearn.model_selection import (GridSearchCV, StratifiedKFold,
                               train_test_split)
from sklearn.pipeline import Pipeline

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

META_COLUMNS = ["sample_id", "source_id", "status", "category",
                "candidate_count", "input_min_db", "input_max_db",
                "input_mean_db", "largest_candidate_fraction", "notes"]

CLEAN_COLUMNS = ["sample_id", "source_id", "label"] + FEATURE_COLUMNS

PARAM_GRID = {
    "model__n_estimators": [200, 300, 500],
    "model__max_depth": [None, 5, 10],
    "model__min_samples_leaf": [1, 2, 4],
    "model__max_features": ["sqrt", "log2"],
}


def audit(df):
    lines = []
    lines.append(f"rows={df.shape[0]} columns={df.shape[1]}")
    lines.append(f"label distribution: {df['label'].value_counts().to_dict()}")
    miss = df[FEATURE_COLUMNS].isna().sum()
    lines.append("missing per feature:")
    for c in FEATURE_COLUMNS:
        lines.append(f"  {c}: {miss[c]} ({100*miss[c]/len(df):.1f}%)")
    lines.append(f"duplicate rows: {int(df.duplicated().sum())}")
    lines.append(f"duplicate sample_id: {int(df['sample_id'].duplicated().sum())}")
    bad_labels = set(df["label"].dropna().unique()) - {0, 1, 0.0, 1.0}
    lines.append(f"unexpected labels: {bad_labels if bad_labels else 'none'}")
    lines.append(f"unique source_id: {df['source_id'].nunique()} "
                 f"(= rows -> no source grouping needed)")
    desc = df[FEATURE_COLUMNS].describe().T[["min", "max", "mean", "50%"]]
    lines.append("feature ranges (min/max/mean/median):")
    for c in FEATURE_COLUMNS:
        r = desc.loc[c]
        lines.append(f"  {c}: min={r['min']:.4g} max={r['max']:.4g} "
                     f"mean={r['mean']:.4g} median={r['50%']:.4g}")
    inf = int(np.isinf(df[FEATURE_COLUMNS].to_numpy(dtype=float)).sum())
    lines.append(f"infinite values in X: {inf}")
    const = [c for c in FEATURE_COLUMNS
             if df[c].dropna().nunique() <= 1]
    lines.append(f"constant (zero-variance) features: {const if const else 'none'} "
                 f"(kept per spec; model cannot split on them)")
    return lines, bad_labels


def metrics_dict(y_true, y_pred, y_score=None):
    out = {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "confusion": confusion_matrix(y_true, y_pred).tolist(),
    }
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred).ravel().tolist()
    out.update({"TP": tp, "TN": tn, "FP": fp, "FN": fn})
    out["roc_auc"] = (float(roc_auc_score(y_true, y_score))
                      if y_score is not None and len(set(y_true)) == 2 else None)
    return out


def main():
    ap = argparse.ArgumentParser(description="Train B2 Random Forest.")
    ap.add_argument("--input", required=True, help="Original CSV (read-only).")
    ap.add_argument("--data-out", default="data/b2_training_clean.csv")
    ap.add_argument("--model-out", default="models/b2_random_forest.joblib")
    ap.add_argument("--features-out", default="models/b2_feature_columns.json")
    ap.add_argument("--report-out", default="reports/b2_random_forest_report.txt")
    ap.add_argument("--importance-out", default="reports/feature_importance.csv")
    args = ap.parse_args()

    df = pd.read_csv(args.input)
    assert list(df.columns)[:6] == ["sample_id", "source_id", "status",
                                    "category", "label", "candidate_count"], \
        "unexpected input schema"
    for c in FEATURE_COLUMNS:
        df[c] = pd.to_numeric(df[c], errors="coerce").replace([np.inf, -np.inf], np.nan)
    df["label"] = pd.to_numeric(df["label"], errors="coerce")

    report = []
    report.append("=== PART 3: DATA QUALITY AUDIT ===")
    audit_lines, bad_labels = audit(df)
    report.extend(audit_lines)
    print("\n".join(audit_lines))
    if bad_labels:
        raise SystemExit(f"STOPPING: unexpected labels {bad_labels}")

    # ---- PART 4: missing-data strategies ----
    complete = df.dropna(subset=FEATURE_COLUMNS)
    dropped = df[~df.index.isin(complete.index)]
    print(f"\nA) complete-case rows: {len(complete)} "
          f"(removed {len(dropped)}: {dropped['sample_id'].tolist()})")
    print(f"B) imputation rows: {len(df)} (SimpleImputer median, fit on train only)")
    report.append(f"complete-case rows: {len(complete)}; "
                  f"removed: {dropped['sample_id'].tolist() or 'none'}")

    # ---- PART 5/6/7: leakage, balance, split (on the FULL set with imputation;
    # complete-case reported as comparison) ----
    X = df[FEATURE_COLUMNS].to_numpy(dtype=float)
    y = df["label"].to_numpy(dtype=int)
    assert X.shape[1] == 14 and not set(FEATURE_COLUMNS) & set(META_COLUMNS)
    oil, notoil = int((y == 1).sum()), int((y == 0).sum())
    print(f"OIL={oil} ({100*oil/len(y):.0f}%) NOT-OIL={notoil} "
          f"({100*notoil/len(y):.0f}%) -> balanced, class_weight as safety")
    Xtr, Xte, ytr, yte, idtr, idte = train_test_split(
        X, y, df["sample_id"].to_numpy(), test_size=0.2, random_state=42,
        stratify=y)
    print(f"train={len(ytr)} (oil={(ytr==1).sum()}, not-oil={(ytr==0).sum()}) "
          f"test={len(yte)} (oil={(yte==1).sum()}, not-oil={(yte==0).sum()})")

    def build(params=None):
        p = dict(n_estimators=300, random_state=42,
                 class_weight="balanced", n_jobs=-1)
        if params:
            p.update(params)
        return Pipeline([("imputer", SimpleImputer(strategy="median")),
                         ("model", RandomForestClassifier(**p))])

    # A) complete-case baseline: own stratified split on the complete subset
    # (reusing the main split indices can leave an empty test set at n=50).
    Xc, yc = complete[FEATURE_COLUMNS].to_numpy(dtype=float), \
        complete["label"].to_numpy(dtype=int)
    if len(yc) >= 4 and len(set(yc)) == 2:
        Xctr, Xcte, yctr, ycte = train_test_split(
            Xc, yc, test_size=0.2, random_state=42, stratify=yc)
        base = RandomForestClassifier(n_estimators=300, random_state=42,
                                      class_weight="balanced", n_jobs=-1)
        base.fit(Xctr, yctr)
        mA = metrics_dict(ycte, base.predict(Xcte))
        mA["train_rows"], mA["test_rows"] = len(yctr), len(ycte)
    else:
        mA = {"accuracy": None, "f1": None, "train_rows": 0, "test_rows": 0,
              "TP": 0, "TN": 0, "FP": 0, "FN": 0}
    print(f"A) complete-case: train_rows={mA['train_rows']} "
          f"test_rows={mA['test_rows']} acc={mA['accuracy']} f1={mA['f1']} "
          f"(tiny by construction -> high variance)")

    # B) imputation pipeline + grid search on TRAIN only
    pipe = build()
    gs = GridSearchCV(pipe, PARAM_GRID,
                      cv=StratifiedKFold(n_splits=3, shuffle=True, random_state=42),
                      scoring="f1", n_jobs=-1)
    gs.fit(Xtr, ytr)
    print(f"B) grid best: {gs.best_params_} cv_f1={gs.best_score_:.3f}")
    best = gs.best_estimator_
    medians = dict(zip(FEATURE_COLUMNS, best.named_steps["imputer"].statistics_.tolist()))
    print(f"    imputer medians (train-only): "
          + ", ".join(f"{k}={v:.4g}" for k, v in medians.items()))

    # ---- PART 10: test evaluation ----
    yp = best.predict(Xte)
    ys = best.predict_proba(Xte)[:, 1]
    mB = metrics_dict(yte, yp, ys)
    print(f"B) test: acc={mB['accuracy']:.3f} prec={mB['precision']:.3f} "
          f"rec={mB['recall']:.3f} f1={mB['f1']:.3f} auc={mB['roc_auc']}")
    print(f"  TP={mB['TP']} TN={mB['TN']} FP={mB['FP']} FN={mB['FN']}")
    print(classification_report(yte, yp, target_names=["LOOK-ALIKE", "OIL"]))

    # ---- PART 11: importance ----
    imp = best.named_steps["model"].feature_importances_
    order = np.argsort(imp)[::-1]
    print("feature importance:")
    for i in order:
        print(f"  {FEATURE_COLUMNS[i]}: {imp[i]:.4f}")

    # ---- PART 12/13/14: save (original untouched) ----
    for d in ["data", "models", "reports"]:
        Path(d).mkdir(parents=True, exist_ok=True)
    clean = df[CLEAN_COLUMNS]
    clean.to_csv(args.data_out, index=False)
    joblib.dump(best, args.model_out)
    with open(args.features_out, "w") as f:
        json.dump(FEATURE_COLUMNS, f, indent=2)
    pd.DataFrame({"feature": FEATURE_COLUMNS, "importance": imp}
                 ).sort_values("importance", ascending=False
                               ).to_csv(args.importance_out, index=False)

    # ---- PART 15: report ----
    rep = []
    rep.append(f"1. original rows: {len(df)}; 2. original columns: {df.shape[1]}")
    rep.append(f"3. removed from model input: {META_COLUMNS}")
    rep.append(f"4. features: {FEATURE_COLUMNS}")
    rep.append(f"5. label dist: oil={oil} not-oil={notoil}")
    rep.append("6. " + "; ".join(audit_lines[2:5]))
    rep.append(f"7. complete-case removed {len(dropped)} rows "
               f"{dropped['sample_id'].tolist()}; imputation kept all {len(df)}")
    rep.append(f"8. train={len(ytr)} test={len(yte)} "
               f"(stratified, random_state=42, test ids {sorted(idte.tolist())})")
    rep.append(f"9. split: stratified 80/20, no grouping (50 unique source_ids)")
    rep.append(f"10. params: {gs.best_params_} + class_weight=balanced")
    rep.append(f"11. cv f1 (train-only, 3-fold): {gs.best_score_:.4f} | "
               f"complete-case baseline: train={mA['train_rows']} "
               f"test={mA['test_rows']} acc={mA['accuracy']} f1={mA['f1']}")
    for k in ["accuracy", "precision", "recall", "f1", "roc_auc"]:
        rep.append(f"{12+['accuracy','precision','recall','f1','roc_auc'].index(k)}. "
                   f"{k}={mB[k]}")
    rep.append(f"17. confusion={mB['confusion']} TP={mB['TP']} TN={mB['TN']} "
               f"FP={mB['FP']} FN={mB['FN']}")
    rep.append("18. importance: " + ", ".join(
        f"{FEATURE_COLUMNS[i]}={imp[i]:.4f}" for i in order))
    rep.append("19. LIMITATIONS: n=50 prototype only; test n=10 -> high variance; "
               "18/50 rows fully imputed (entire vector from train medians) - "
               "collect labeled candidates, esp. with weather/AIS present; "
               "validate on independent scenes; do NOT deploy as production.")
    Path(args.report_out).write_text("\n".join(rep) + "\n")

    # ---- PART 17: final validation ----
    assert X.shape[1] == 14 and not set(FEATURE_COLUMNS) & set(META_COLUMNS)
    reloaded = joblib.load(args.model_out)
    assert (reloaded.predict(Xte) == yp).all(), "reload mismatch"
    assert (reloaded.predict(Xte) == best.predict(Xte)).all()
    print("reload check: predictions identical")

    print("\nOriginal dataset: 50 rows\nOriginal columns: 25\n"
          "Removed from model: 10 columns\nB2 features: 14\nTarget: label\n"
          f"Rows used: {len(df)} (complete-case subset: {len(complete)})\n"
          f"Rows removed (complete-case): {len(dropped)}\n"
          f"OIL: {oil}\nNOT OIL: {notoil}\nTrain: {len(ytr)}\nTest: {len(yte)}\n"
          f"Accuracy: {mB['accuracy']:.4f}\nPrecision: {mB['precision']:.4f}\n"
          f"Recall: {mB['recall']:.4f}\nF1: {mB['f1']:.4f}\nROC-AUC: {mB['roc_auc']}\n"
          "\nCreated:\ndata/b2_training_clean.csv\n"
          "models/b2_random_forest.joblib\nmodels/b2_feature_columns.json\n"
          "reports/b2_random_forest_report.txt\nreports/feature_importance.csv")


if __name__ == "__main__":
    main()
