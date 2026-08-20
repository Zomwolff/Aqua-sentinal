import asyncio
import logging
import pandas as pd
import joblib
import os
import time
from datetime import datetime, timezone
from sklearn.ensemble import IsolationForest
import sys
from typing import Dict, Any, List

sys.path.insert(0, "/app")
from shared.db import get_pool

log = logging.getLogger(__name__)

MODEL_PATH = "/tmp/isolation_forest.joblib"
TRAIN_INTERVAL_S = 3600  # Train every hour
MIN_SAMPLES = 100

FEATURES = [
    "avg_speed", "speed_variance", "max_speed", "course_variance",
    "heading_change_rate", "loitering_score", "max_rate_of_turn_deg_min", "draught_change_m"
]

def _fill_na(df: pd.DataFrame) -> pd.DataFrame:
    for col in FEATURES:
        if col in df.columns:
            df[col] = df[col].fillna(0.0)
        else:
            df[col] = 0.0
    return df

async def train_isolation_forest():
    """Fetch the last 24 hours of vessel_features and retrain the Isolation Forest."""
    try:
        pool = await get_pool()
        # Fetch last 24h data
        rows = await pool.fetch(
            f"SELECT {', '.join(FEATURES)} FROM vessel_features WHERE window_end >= NOW() - INTERVAL '24 hours'"
        )
        if len(rows) < MIN_SAMPLES:
            log.info("Not enough data to train Isolation Forest (%d samples). Skipping.", len(rows))
            return
        
        df = pd.DataFrame([dict(r) for r in rows])
        df = _fill_na(df)

        model = IsolationForest(n_estimators=100, contamination=0.005, random_state=42)
        model.fit(df[FEATURES])

        joblib.dump(model, MODEL_PATH)
        log.info("Successfully trained and saved Isolation Forest on %d samples.", len(rows))
    except Exception as e:
        log.error("Failed to train Isolation Forest: %s", e)

async def ml_training_loop():
    """Background task to retrain the ML model periodically."""
    log.info("Starting ML training loop...")
    while True:
        await train_isolation_forest()
        await asyncio.sleep(TRAIN_INTERVAL_S)

def predict_anomaly(features: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run features through the Isolation Forest if it exists."""
    if not os.path.exists(MODEL_PATH):
        return []

    try:
        model = joblib.load(MODEL_PATH)
        
        # Prepare single row DataFrame
        row = {f: float(features.get(f) or 0.0) for f in FEATURES}
        df = pd.DataFrame([row])
        df = _fill_na(df)
        
        prediction = model.predict(df[FEATURES])[0] # 1 for normal, -1 for anomaly
        
        if prediction == -1:
            score = model.score_samples(df[FEATURES])[0]
            # Convert negative score to positive confidence (0 to 1 roughly)
            confidence = min(0.95, max(0.5, abs(score) * 2))
            
            return [{
                "mmsi": features.get("mmsi"),
                "window_start": features.get("window_start"),
                "anomaly_type": "ml_behavioral_anomaly",
                "severity": "HIGH",
                "source": "isolation_forest",
                "confidence_score": confidence,
                "evidence": {
                    "ml_model": "isolation_forest",
                    "anomaly_score": float(score),
                    "rule": "unsupervised_anomaly_detected"
                }
            }]
    except Exception as e:
        log.error("Failed to predict using Isolation Forest: %s", e)
    
    return []
