from app.incident_report import build_incident_report, source_distribution


def test_source_shares_include_unknown_and_sum_to_100():
    result = source_distribution([
        {"mmsi": "A", "vessel_name": "Vessel A", "final_score": 0.87},
        {"mmsi": "B", "vessel_name": "Vessel B", "final_score": 0.08},
        {"mmsi": "C", "vessel_name": "Vessel C", "final_score": 0.03},
    ])
    assert [item["relative_likelihood_percent"] for item in result] == [87, 8, 3, 2]
    assert sum(item["relative_likelihood_percent"] for item in result) == 100


def test_report_preserves_observed_values_and_provenance():
    report = build_incident_report(
        {
            "id": "spill-1", "detected_at": "2026-08-22T00:00:00Z",
            "latitude": 18.9, "longitude": 72.8, "area_km2": 12.4,
            "confidence": 0.87, "source": "sar", "source_image_id": "scene-1",
        },
        {
            "severity_level": "CRITICAL", "score": 0.88,
            "environmental_risk": 0.74, "protected_area_risk": 0.81,
            "population_risk": 0.62,
        },
        [], [], [],
    )
    assert report["assessment"]["detection_confidence_percent"] == 87
    assert report["assessment"]["spill_area_km2"] == 12.4
    assert report["assessment"]["ecological_risk"]["level"] == "CRITICAL"
    assert report["provenance"]["is_synthetic"] is False
