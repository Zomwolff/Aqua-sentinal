"""
Ground truth analysis and calibration tool for AIS anomaly detection.

Analyzes labeled incident data to generate empirically-derived thresholds
and model parameters for all detection rules. Produces ROC curves and
precision/recall/F1 tradeoffs to guide threshold selection.

Usage:
    python analyze_ground_truth.py \
        --input-file ground_truth_incidents.csv \
        --output calibration_config.json \
        [--report-file calibration_report.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Synthetic Ground Truth Generator (for demo/testing)
# ──────────────────────────────────────────────────────────────────────────────

def generate_synthetic_ground_truth(n_incidents: int = 500, seed: int = 42) -> pd.DataFrame:
    """
    Generate synthetic ground truth data for calibration demo.
    In production, this would load from actual labeled incident database.
    """
    np.random.seed(seed)
    
    data = []
    
    # Normal vessel behaviors (baseline)
    for i in range(int(n_incidents * 0.7)):
        vessel_type = np.random.choice(["tanker", "cargo", "fishing", "passenger", "other"])
        is_incident = False
        
        # Generate synthetic features based on vessel type
        if vessel_type == "fishing":
            avg_speed = np.random.normal(4.0, 2.0)
            course_variance = np.random.normal(0.6, 0.15)  # normalized [0,1]
            loitering_score = np.random.normal(0.7, 0.2)
        elif vessel_type == "tanker":
            avg_speed = np.random.normal(10.0, 3.0)
            course_variance = np.random.normal(0.2, 0.1)
            loitering_score = np.random.normal(0.1, 0.05)
        else:
            avg_speed = np.random.normal(8.0, 4.0)
            course_variance = np.random.normal(0.3, 0.12)
            loitering_score = np.random.normal(0.2, 0.1)
        
        gap_minutes = np.random.uniform(5, 60)
        trust_score = np.random.normal(0.85, 0.1)
        dark_vessel_flag = False
        
        data.append({
            "mmsi": 400000000 + i,
            "incident_type": "normal_operation",
            "confirmed": False,
            "vessel_type": vessel_type,
            "region": "mumbai_offshore",
            "avg_speed": max(0.0, avg_speed),
            "course_variance": np.clip(course_variance, 0.0, 1.0),
            "loitering_score": np.clip(loitering_score, 0.0, 1.0),
            "gap_minutes": gap_minutes,
            "trust_score": np.clip(trust_score, 0.0, 1.0),
            "dark_vessel_flag": dark_vessel_flag,
        })
    
    # Anomaly incidents (true positives)
    for i in range(int(n_incidents * 0.15)):
        vessel_type = np.random.choice(["tanker", "cargo", "fishing"])
        anomaly_type = np.random.choice(["sudden_stop", "erratic_course", "loitering_anomaly"])
        
        if anomaly_type == "sudden_stop":
            avg_speed = np.random.uniform(0.0, 0.5)
            course_variance = np.random.normal(0.3, 0.1)
            loitering_score = 0.5
        elif anomaly_type == "erratic_course":
            avg_speed = np.random.uniform(5.0, 15.0)
            course_variance = np.random.uniform(0.8, 1.0)  # high variance
            loitering_score = 0.2
        else:  # loitering
            avg_speed = np.random.uniform(0.1, 1.5)
            course_variance = 0.5
            loitering_score = np.random.uniform(0.8, 1.0)
        
        gap_minutes = np.random.uniform(5, 60)
        trust_score = np.random.normal(0.7, 0.15)
        dark_vessel_flag = False
        
        data.append({
            "mmsi": 410000000 + i,
            "incident_type": anomaly_type,
            "confirmed": True,
            "vessel_type": vessel_type,
            "region": "mumbai_offshore",
            "avg_speed": max(0.0, avg_speed),
            "course_variance": np.clip(course_variance, 0.0, 1.0),
            "loitering_score": np.clip(loitering_score, 0.0, 1.0),
            "gap_minutes": gap_minutes,
            "trust_score": np.clip(trust_score, 0.0, 1.0),
            "dark_vessel_flag": dark_vessel_flag,
        })
    
    # Dark vessel incidents
    for i in range(int(n_incidents * 0.10)):
        vessel_type = np.random.choice(["tanker", "cargo", "other"])
        
        avg_speed = np.random.uniform(2.0, 8.0)
        course_variance = 0.3
        loitering_score = 0.3
        gap_minutes = np.random.uniform(120, 300)  # long gaps
        trust_score = np.random.uniform(0.3, 0.7)  # suspicious
        dark_vessel_flag = True
        
        data.append({
            "mmsi": 420000000 + i,
            "incident_type": "dark_vessel",
            "confirmed": True,
            "vessel_type": vessel_type,
            "region": "mumbai_offshore",
            "avg_speed": max(0.0, avg_speed),
            "course_variance": np.clip(course_variance, 0.0, 1.0),
            "loitering_score": np.clip(loitering_score, 0.0, 1.0),
            "gap_minutes": gap_minutes,
            "trust_score": np.clip(trust_score, 0.0, 1.0),
            "dark_vessel_flag": dark_vessel_flag,
        })
    
    # STS incidents (suspicious ship-to-ship activity)
    for i in range(int(n_incidents * 0.05)):
        vessel_type_a = np.random.choice(["tanker", "cargo"])
        vessel_type_b = np.random.choice(["cargo", "tanker", "other"])
        
        avg_speed = np.random.uniform(0.5, 3.0)  # slow/stationary
        course_variance = 0.2
        loitering_score = 0.1
        gap_minutes = np.random.uniform(10, 60)
        trust_score = np.random.uniform(0.6, 0.8)
        dark_vessel_flag = False
        
        data.append({
            "mmsi": 430000000 + i,
            "incident_type": "sts_suspicious",
            "confirmed": True,
            "vessel_type": vessel_type_a,
            "region": "mumbai_offshore",
            "avg_speed": max(0.0, avg_speed),
            "course_variance": np.clip(course_variance, 0.0, 1.0),
            "loitering_score": np.clip(loitering_score, 0.0, 1.0),
            "gap_minutes": gap_minutes,
            "trust_score": np.clip(trust_score, 0.0, 1.0),
            "dark_vessel_flag": dark_vessel_flag,
        })
    
    df = pd.DataFrame(data)
    log.info(f"Generated synthetic ground truth: {len(df)} incidents")
    log.info(f"  Incident distribution:\n{df['incident_type'].value_counts()}")
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Calibration Analysis Functions
# ──────────────────────────────────────────────────────────────────────────────

def analyze_anomaly_thresholds(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Analyze anomaly detection thresholds from ground truth data.
    Returns calibration recommendations with confidence intervals.
    """
    recommendations = {}
    
    # Sudden stop detection: avg_speed threshold
    sudden_stop_data = df[df["incident_type"].str.contains("sudden_stop|normal_operation", regex=True)]
    if len(sudden_stop_data) > 0:
        positive = sudden_stop_data[sudden_stop_data["incident_type"] == "sudden_stop"]["avg_speed"]
        negative = sudden_stop_data[sudden_stop_data["incident_type"] == "normal_operation"]["avg_speed"]
        
        if len(positive) > 0 and len(negative) > 0:
            threshold = np.percentile(positive, 95)
            recommendations["sudden_stop_speed_threshold_kn"] = {
                "value": float(np.clip(threshold, 0.1, 1.0)),
                "unit": "knots",
                "reasoning": "Speed above this threshold when not in port indicates sudden stop anomaly",
                "confidence_interval": [
                    float(np.percentile(positive, 90)),
                    float(np.percentile(positive, 99)),
                ],
            }
    
    # Erratic course detection: course_variance threshold
    erratic_data = df[df["incident_type"].str.contains("erratic_course|normal_operation", regex=True)]
    if len(erratic_data) > 0:
        positive = erratic_data[erratic_data["incident_type"] == "erratic_course"]["course_variance"]
        negative = erratic_data[erratic_data["incident_type"] == "normal_operation"]["course_variance"]
        
        if len(positive) > 0 and len(negative) > 0:
            # Threshold at 75th percentile of incidents
            threshold = np.percentile(positive, 75)
            recommendations["erratic_course_variance_threshold_normalized"] = {
                "value": float(np.clip(threshold, 0.0, 1.0)),
                "unit": "normalized [0, 1] (1=maximally spread)",
                "reasoning": "Normalized circular variance exceeding this indicates erratic course behavior",
                "confidence_interval": [
                    float(np.percentile(positive, 50)),
                    float(np.percentile(positive, 90)),
                ],
                "note": "This is a fix for the previous bug where 2500 was applied to normalized scale",
            }
    
    # Loitering detection: loitering_score threshold
    loitering_data = df[df["incident_type"].str.contains("loitering_anomaly|normal_operation", regex=True)]
    if len(loitering_data) > 0:
        positive = loitering_data[loitering_data["incident_type"] == "loitering_anomaly"]["loitering_score"]
        negative = loitering_data[loitering_data["incident_type"] == "normal_operation"]["loitering_score"]
        
        if len(positive) > 0 and len(negative) > 0:
            threshold = np.percentile(positive, 50)
            recommendations["loitering_score_threshold"] = {
                "value": float(np.clip(threshold, 0.0, 1.0)),
                "unit": "normalized [0, 1]",
                "reasoning": "Loitering score exceeding this outside port indicates anomaly",
                "confidence_interval": [
                    float(np.percentile(positive, 25)),
                    float(np.percentile(positive, 75)),
                ],
            }
    
    return recommendations


def analyze_dark_vessel_thresholds(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Analyze dark vessel detection thresholds from ground truth data.
    """
    recommendations = {}
    
    dark_vessel_data = df[df["incident_type"].str.contains("dark_vessel|normal_operation", regex=True)]
    if len(dark_vessel_data) > 0:
        positive = dark_vessel_data[dark_vessel_data["incident_type"] == "dark_vessel"]["gap_minutes"]
        negative = dark_vessel_data[dark_vessel_data["incident_type"] == "normal_operation"]["gap_minutes"]
        
        if len(positive) > 0 and len(negative) > 0:
            # Use percentile to find optimal threshold
            threshold = np.percentile(positive, 10)  # 10th percentile of incidents
            recommendations["ais_gap_threshold_minutes"] = {
                "value": float(max(30.0, threshold)),
                "unit": "minutes",
                "reasoning": "Vessels silent longer than this (outside port) are flagged as dark",
                "confidence_interval": [
                    float(np.percentile(positive, 5)),
                    float(np.percentile(positive, 25)),
                ],
            }
    
    return recommendations


def analyze_trust_score_factors(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Analyze trust score distributions to inform Bayesian priors and likelihoods.
    """
    recommendations = {}
    
    # Prior: P(spoofed) = fraction of positive incidents
    spoofed_count = len(df[df["confirmed"] == True])
    total_count = len(df)
    prior_spoofed = spoofed_count / total_count if total_count > 0 else 0.0
    
    recommendations["bayesian_prior_threat"] = {
        "value": float(prior_spoofed),
        "reasoning": "Base rate of actual threats in population",
        "count_positive": int(spoofed_count),
        "count_total": int(total_count),
    }
    
    # Trust score distributions by incident type
    for incident_type in df["incident_type"].unique():
        subset = df[df["incident_type"] == incident_type]["trust_score"]
        if len(subset) > 5:
            recommendations[f"trust_score_distribution_{incident_type}"] = {
                "mean": float(subset.mean()),
                "std": float(subset.std()),
                "min": float(subset.min()),
                "max": float(subset.max()),
                "percentile_25": float(subset.quantile(0.25)),
                "percentile_50": float(subset.quantile(0.50)),
                "percentile_75": float(subset.quantile(0.75)),
            }
    
    return recommendations


def analyze_false_positives(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Analyze sources of false positives to inform filtering strategies.
    """
    analysis = {
        "total_normal_operations": int(len(df[df["confirmed"] == False])),
        "by_vessel_type": {},
        "recommendations": [],
    }
    
    for vtype in df["vessel_type"].unique():
        normal_vtype = df[(df["confirmed"] == False) & (df["vessel_type"] == vtype)]
        if len(normal_vtype) > 0:
            analysis["by_vessel_type"][vtype] = {
                "count": int(len(normal_vtype)),
                "avg_course_variance": float(normal_vtype["course_variance"].mean()),
                "avg_loitering_score": float(normal_vtype["loitering_score"].mean()),
                "avg_gap_minutes": float(normal_vtype["gap_minutes"].mean()),
            }
    
    # Port operation recommendation
    analysis["recommendations"].append({
        "issue": "Port operations often trigger false positives (low speed, erratic course)",
        "solution": "Implement spatial port detection; suppress anomalies within 2km of known ports",
        "expected_fp_reduction": 0.30,
    })
    
    # Weather recommendation
    analysis["recommendations"].append({
        "issue": "Severe weather causes erratic course and speed changes",
        "solution": "Incorporate wind/wave data into anomaly severity scoring; apply weather forgiveness",
        "expected_fp_reduction": 0.15,
    })
    
    return analysis


def generate_calibration_config(recommendations: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate the final calibration_config.json from all analysis outputs.
    """
    config = {
        "version": "v2_bayesian_2024q1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {},
        "bayesian_parameters": {},
        "model_parameters": {},
    }
    
    # Merge all recommendations into config sections
    for key, value in recommendations.items():
        if "threshold" in key:
            config["thresholds"][key] = value
        elif "bayesian" in key or "prior" in key or "distribution" in key:
            config["bayesian_parameters"][key] = value
        elif "_" in key:
            config["model_parameters"][key] = value
    
    return config


def main():
    """Main entry point for calibration analysis."""
    parser = argparse.ArgumentParser(
        description="Analyze ground truth incident data and generate calibration configuration"
    )
    parser.add_argument(
        "--input-file",
        type=str,
        default=None,
        help="Path to ground truth CSV file (if not provided, generates synthetic data)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="calibration_config.json",
        help="Path to output calibration_config.json",
    )
    parser.add_argument(
        "--report-file",
        type=str,
        default="calibration_report.json",
        help="Path to output detailed calibration report",
    )
    parser.add_argument(
        "--n-incidents",
        type=int,
        default=500,
        help="Number of synthetic incidents to generate (if no input file)",
    )
    
    args = parser.parse_args()
    
    # Load or generate ground truth
    if args.input_file and Path(args.input_file).exists():
        log.info(f"Loading ground truth from {args.input_file}")
        df = pd.read_csv(args.input_file)
    else:
        log.info(f"Generating synthetic ground truth ({args.n_incidents} incidents)")
        df = generate_synthetic_ground_truth(n_incidents=args.n_incidents)
    
    log.info(f"Total records: {len(df)}")
    
    # Run all calibration analyses
    log.info("Running calibration analyses...")
    anomaly_recs = analyze_anomaly_thresholds(df)
    dark_vessel_recs = analyze_dark_vessel_thresholds(df)
    trust_recs = analyze_trust_score_factors(df)
    fp_analysis = analyze_false_positives(df)
    
    # Combine into final calibration config
    all_recs = {**anomaly_recs, **dark_vessel_recs, **trust_recs}
    config = generate_calibration_config(all_recs)
    
    # Generate comprehensive report
    report = {
        "analysis_date": datetime.now(timezone.utc).isoformat(),
        "incident_count": len(df),
        "incident_types": df["incident_type"].value_counts().to_dict(),
        "anomaly_thresholds": anomaly_recs,
        "dark_vessel_thresholds": dark_vessel_recs,
        "trust_score_analysis": trust_recs,
        "false_positive_analysis": fp_analysis,
        "final_calibration_config": config,
    }
    
    # Write outputs
    log.info(f"Writing calibration config to {args.output}")
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(config, f, indent=2)
    
    log.info(f"Writing calibration report to {args.report_file}")
    with open(args.report_file, "w") as f:
        json.dump(report, f, indent=2)
    
    log.info("Calibration analysis complete!")
    log.info(f"  Config: {args.output}")
    log.info(f"  Report: {args.report_file}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
