"""
Anomaly Detection ML Model — Production-grade Isolation Forest.

Key improvements:
  1. Persistent model storage on a named Docker volume (/data/models) — survives restarts.
  2. Versioned model metadata (trained_at, sample_count) to detect staleness.
  3. Warm-start on container boot: loads existing model if not stale.
  4. Severity calibrated against actual anomaly score percentile distribution.
  5. Thread-safe model swapping so inference and training can run concurrently.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

sys.path.insert(0, "/app")
from shared.db import get_pool

log = logging.getLogger(__name__)

# ── Configuration (all overridable via env) ─────────────────────────────────
MODEL_DIR         = os.environ.get("ML_MODEL_DIR", "/data/models")
MODEL_PATH        = os.path.join(MODEL_DIR, "isolation_forest.joblib")
META_PATH         = os.path.join(MODEL_DIR, "model_meta.json")
TRAIN_INTERVAL_S  = int(os.environ.get("ML_TRAIN_INTERVAL_S", 3600))
MAX_MODEL_AGE_H   = float(os.environ.get("ML_MAX_MODEL_AGE_H", 25))
MIN_SAMPLES       = int(os.environ.get("ML_MIN_SAMPLES", 100))

FEATURES = [
    "avg_speed", "speed_variance", "max_speed", "course_variance",
    "heading_change_rate", "loitering_score", "max_rate_of_turn_deg_min", "draught_change_m",
]

# ── Thread-safe model state ─────────────────────────────────────────────────
_model_lock  = threading.Lock()
_current_model: Optional[IsolationForest] = None
_score_thresholds: Dict[str, float] = {}


def _ensure_model_dir() -> None:
    os.makedirs(MODEL_DIR, exist_ok=True)


def _load_meta() -> Optional[Dict[str, Any]]:
    if not os.path.exists(META_PATH):
        return None
    try:
        with open(META_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def _save_meta(sample_count: int, thresholds: Dict[str, float]) -> None:
    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "sample_count": sample_count,
        "feature_names": FEATURES,
        "score_thresholds": thresholds,
    }
    with open(META_PATH, "w") as f:
        json.dump(meta, f, indent=2)
    log.info("Saved model metadata -> %s", META_PATH)


def _is_model_stale(meta: Optional[Dict[str, Any]]) -> bool:
    if meta is None or not os.path.exists(MODEL_PATH):
        return True
    try:
        trained_at = datetime.fromisoformat(meta["trained_at"])
        if trained_at.tzinfo is None:
            trained_at = trained_at.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - trained_at).total_seconds() / 3600.0
        return age_h > MAX_MODEL_AGE_H
    except Exception:
        return True


def _fill_na(df: pd.DataFrame) -> pd.DataFrame:
    for col in FEATURES:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        else:
            df[col] = 0.0
    return df


def _compute_thresholds(model: IsolationForest, X: pd.DataFrame) -> Dict[str, float]:
    """Calibrate severity thresholds from training data score distribution."""
    scores = model.score_samples(X[FEATURES].values)
    return {
        "LOW":    float(np.percentile(scores, 5)),
        "MEDIUM": float(np.percentile(scores, 2)),
        "HIGH":   float(np.percentile(scores, 0.5)),
    }


def _try_warm_start() -> None:
    _ensure_model_dir()
    meta = _load_meta()
    if not _is_model_stale(meta):
        try:
            model = joblib.load(MODEL_PATH)
            thresholds = meta.get("score_thresholds", {})
            with _model_lock:
                global _current_model, _score_thresholds
                _current_model = model
                _score_thresholds = thresholds
            log.info(
                "Warm-start: loaded Isolation Forest trained at %s (%d samples)",
                meta.get("trained_at"), meta.get("sample_count", 0),
            )
        except Exception as e:
            log.warning("Warm-start failed, will retrain: %s", e)
    else:
        if meta:
            log.info("Existing model stale (>%.0fh). Will retrain on first cycle.", MAX_MODEL_AGE_H)
        else:
            log.info("No existing model found. Will train on first cycle.")


_try_warm_start()


async def train_isolation_forest() -> None:
    """Fetch 48h of vessel_features and retrain. Saves to persistent volume."""
    try:
        pool = await get_pool()
        cols = ", ".join(FEATURES)
        rows = await pool.fetch(
            f"SELECT {cols} FROM vessel_features WHERE window_end >= NOW() - INTERVAL '48 hours'"
        )
        n = len(rows)
        if n < MIN_SAMPLES:
            log.info("Not enough data to train Isolation Forest (%d / %d samples). Skipping.", n, MIN_SAMPLES)
            return

        df = pd.DataFrame([dict(r) for r in rows])
        df = _fill_na(df)

        model = IsolationForest(
            n_estimators=200, contamination=0.005,
            max_features=1.0, bootstrap=False,
            random_state=42, n_jobs=-1,
        )
        model.fit(df[FEATURES])
        thresholds = _compute_thresholds(model, df)

        # Atomic persist: write temp then rename to avoid partial writes
        _ensure_model_dir()
        tmp = MODEL_PATH + ".tmp"
        joblib.dump(model, tmp)
        os.replace(tmp, MODEL_PATH)
        _save_meta(n, thresholds)

        with _model_lock:
            global _current_model, _score_thresholds
            _current_model = model
            _score_thresholds = thresholds

        log.info(
            "Isolation Forest retrained on %d samples. Thresholds: LOW=%.4f MEDIUM=%.4f HIGH=%.4f",
            n, thresholds["LOW"], thresholds["MEDIUM"], thresholds["HIGH"],
        )
    except Exception as e:
        log.error("Failed to train Isolation Forest: %s", e)


async def ml_training_loop() -> None:
    log.info("ML training loop started (interval=%ds).", TRAIN_INTERVAL_S)
    while True:
        await train_isolation_forest()
        await asyncio.sleep(TRAIN_INTERVAL_S)


def _score_to_severity(score: float) -> str:
    with _model_lock:
        thresholds = dict(_score_thresholds)
    if not thresholds:
        return "MEDIUM"
    if score <= thresholds.get("HIGH", -0.20):
        return "HIGH"
    elif score <= thresholds.get("MEDIUM", -0.15):
        return "MEDIUM"
    return "LOW"


def predict_anomaly(features: Dict[str, Any]) -> List[Dict[str, Any]]:
    with _model_lock:
        model = _current_model
    if model is None:
        return []

    try:
        row = {f: float(features.get(f) or 0.0) for f in FEATURES}
        X = _fill_na(pd.DataFrame([row]))
        prediction = model.predict(X[FEATURES].values)[0]
        if prediction != -1:
            return []

        score = float(model.score_samples(X[FEATURES].values)[0])
        severity = _score_to_severity(score)
        raw_conf = min(0.97, max(0.50, 0.50 + abs(score) * 2.5))

        return [{
            "mmsi":             features.get("mmsi"),
            "window_start":     features.get("window_start"),
            "anomaly_type":     "ml_behavioral_anomaly",
            "severity":         severity,
            "source":           "isolation_forest",
            "confidence_score": round(raw_conf, 3),
            "evidence": {
                "ml_model":           "isolation_forest",
                "anomaly_score":      round(score, 5),
                "severity_thresholds": _score_thresholds,
                "rule":               "unsupervised_anomaly_detected",
            },
        }]
    except Exception as e:
        log.error("Isolation Forest inference error: %s", e)
        return []

