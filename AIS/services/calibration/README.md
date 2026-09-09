# AIS Scoring Calibration Framework

This directory contains data-driven calibration tools and configurations for the Aqua-Sentinel AIS scoring and anomaly detection systems.

## Purpose

Replace arbitrary thresholds and weights with empirically-derived values based on ground truth incident data and statistical analysis.

## Key Files

- `analyze_ground_truth.py` — Analyzes ground truth incidents and baseline vessel behaviors to generate calibration recommendations
- `calibration_config.json` — Active calibration configuration with all thresholds and model parameters
- `ground_truth_schema.sql` — Database schema for storing labeled incident data
- `calibration_report.json` — Generated report from latest calibration analysis (ROC curves, precision/recall, recommendations)

## Calibration Process

1. Load ground truth incident dataset (labeled positive/negative examples)
2. Compute statistics for each anomaly type, stratified by vessel type/region/season
3. Generate ROC curves and precision/recall tradeoffs for each detection rule
4. Recommend optimal thresholds based on business requirements (false positive cost vs missed incident cost)
5. Store recommended thresholds in `calibration_config.json`
6. Load config at service startup

## Usage

```bash
# Analyze ground truth data and generate calibration recommendations
python analyze_ground_truth.py --input-file ground_truth_incidents.csv --output calibration_config.json

# View latest calibration report
cat calibration_report.json
```

## Data Requirements

Ground truth incidents should include:
- `mmsi` — vessel identifier
- `incident_type` — e.g., "sudden_stop", "erratic_course", "spoofing", "dark_vessel", "sts_illegal"
- `confirmed` — boolean, whether incident was confirmed
- `vessel_type` — cargo, tanker, fishing, etc.
- `region` — geographic region
- `season` — if seasonal patterns exist
- Additional features used by anomaly detection rules

## Notes

- Thresholds are empirically derived from data, not arbitrary
- Each threshold is documented with confidence intervals and recommendations
- Calibration should be re-run periodically as new incident data becomes available
- All thresholds are versioned for reproducibility and audit trails
