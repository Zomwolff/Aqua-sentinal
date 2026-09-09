"""
Unit tests for Severity-Impact V1 rule-based calculation logic.

Tests the pure functions in app/severity.py without dependencies on DB/Redis.
"""
import pytest
from app.severity import (
    normalize_severity,
    to_db_severity,
    compare_severity,
    max_severity,
    determine_ecological_severity,
    determine_socioeconomic_severity,
    apply_escalation_rules,
    calculate_severity_score,
    calculate_urgency_factor,
    determine_trajectory,
    SCORE_BANDS,
)


class TestDatabaseSeverityMapping:
    """Test internal to database severity level mapping."""
    
    def test_to_db_severity_mappings(self):
        """Verify correct mapping to database enum values."""
        assert to_db_severity("None") == "LOW"
        assert to_db_severity("Low") == "LOW"
        assert to_db_severity("Medium") == "MODERATE"  # KEY: Medium → MODERATE
        assert to_db_severity("High") == "HIGH"
        assert to_db_severity("Critical") == "CRITICAL"
    
    def test_to_db_severity_never_returns_medium(self):
        """Ensure MEDIUM is never returned (must be MODERATE)."""
        # Medium should map to MODERATE
        result = to_db_severity("Medium")
        assert result == "MODERATE"
        assert result != "MEDIUM"
    
    def test_to_db_severity_none_maps_to_low(self):
        """Verify None maps to LOW (not NONE)."""
        assert to_db_severity("None") == "LOW"
    
    def test_to_db_severity_default(self):
        """Test fallback for unknown values."""
        assert to_db_severity("unknown") == "LOW"
        assert to_db_severity("") == "LOW"
        assert to_db_severity(None) == "LOW"


class TestSeverityNormalization:
    """Test severity level normalization and comparison."""
    
    def test_normalize_severity(self):
        assert normalize_severity("LOW") == "Low"
        assert normalize_severity("MODERATE") == "Medium"
        assert normalize_severity("HIGH") == "High"
        assert normalize_severity("CRITICAL") == "Critical"
        assert normalize_severity("None") == "None"
        assert normalize_severity("Low") == "Low"
    
    def test_compare_severity(self):
        assert compare_severity("Low", "High") == -1
        assert compare_severity("High", "Low") == 1
        assert compare_severity("Medium", "Medium") == 0
        assert compare_severity("None", "Low") == -1
        assert compare_severity("Critical", "High") == 1
    
    def test_max_severity(self):
        assert max_severity(["Low", "Medium", "High"]) == "High"
        assert max_severity(["None", "Low"]) == "Low"
        assert max_severity(["Critical", "High", "Medium"]) == "Critical"
        assert max_severity([]) == "None"
        assert max_severity(["None"]) == "None"


class TestEcologicalSeverity:
    """Test ecological severity determination."""
    
    def test_no_ecological_impacts(self):
        severity, high_crit, affected = determine_ecological_severity([])
        assert severity == "None"
        assert high_crit == []
        assert affected == 0
    
    def test_single_low_impact(self):
        impacts = [
            {"category": "Low", "receptor_type": "mangrove"},
        ]
        severity, high_crit, affected = determine_ecological_severity(impacts)
        assert severity == "Low"
        assert high_crit == []
        assert affected == 1
    
    def test_multiple_mixed_impacts(self):
        impacts = [
            {"category": "Low", "receptor_type": "mangrove"},
            {"category": "High", "receptor_type": "coral_reef"},
            {"category": "Medium", "receptor_type": "mpa"},
        ]
        severity, high_crit, affected = determine_ecological_severity(impacts)
        assert severity == "High"
        assert "coral_reef" in high_crit
        assert len(high_crit) == 1
        assert affected == 3
    
    def test_critical_impact_dominates(self):
        impacts = [
            {"category": "Low", "receptor_type": "mangrove"},
            {"category": "Critical", "receptor_type": "coral_reef"},
            {"category": "Medium", "receptor_type": "mpa"},
        ]
        severity, high_crit, affected = determine_ecological_severity(impacts)
        assert severity == "Critical"
        assert "coral_reef" in high_crit
        assert affected == 3
    
    def test_multiple_high_critical_receptors(self):
        impacts = [
            {"category": "High", "receptor_type": "mangrove"},
            {"category": "Critical", "receptor_type": "coral_reef"},
            {"category": "High", "receptor_type": "mpa"},
            {"category": "Medium", "receptor_type": "sensitive_coastline"},
        ]
        severity, high_crit, affected = determine_ecological_severity(impacts)
        assert severity == "Critical"
        assert len(high_crit) == 3
        assert "mangrove" in high_crit
        assert "coral_reef" in high_crit
        assert "mpa" in high_crit
        assert affected == 4

    def test_duplicate_high_records_same_receptor_count_as_one(self):
        """Two High+ records for the same receptor type must count as ONE distinct type.
        
        Verifies the audit finding C-4 fix: breadth escalation requires ≥2 DISTINCT
        receptor types at High+, not ≥2 records.
        """
        impacts = [
            {"category": "High",     "receptor_type": "mangrove"},
            {"category": "Critical", "receptor_type": "mangrove"},  # same type, different category
            {"category": "High",     "receptor_type": "mangrove"},  # exact duplicate
        ]
        severity, high_crit, affected = determine_ecological_severity(impacts)
        assert severity == "Critical"
        # Only one distinct receptor type at High+ → no breadth escalation
        assert len(high_crit) == 1
        assert high_crit == ["mangrove"]
        # Breadth escalation must NOT fire with count=1
        from app.severity import apply_escalation_rules
        final, applied, reasons = apply_escalation_rules(
            "High", "High", "Low", len(high_crit), None
        )
        assert final == "High"
        assert applied is False

    def test_two_distinct_high_receptor_types_trigger_breadth(self):
        """Two different receptor types at High+ → breadth escalation fires.
        
        Companion to test_duplicate_high_records_same_receptor_count_as_one.
        """
        impacts = [
            {"category": "High", "receptor_type": "mangrove"},
            {"category": "High", "receptor_type": "coral_reef"},
        ]
        severity, high_crit, affected = determine_ecological_severity(impacts)
        assert severity == "High"
        assert len(high_crit) == 2
        assert set(high_crit) == {"mangrove", "coral_reef"}
        # Breadth escalation MUST fire with 2 distinct types
        from app.severity import apply_escalation_rules
        final, applied, reasons = apply_escalation_rules(
            "High", "High", "Low", len(high_crit), None
        )
        assert final == "Critical"
        assert applied is True
        assert any("multiple_high_ecological" in r for r in reasons)


class TestSocioeconomicSeverity:
    """Test socioeconomic severity determination."""
    
    def test_no_receptors(self):
        severity, drivers = determine_socioeconomic_severity(0, 0)
        assert severity == "Low"
        assert "no_socioeconomic_receptors_nearby" in drivers
    
    def test_port_within_5km(self):
        severity, drivers = determine_socioeconomic_severity(1, 0)
        assert severity == "High"
        assert any("port_within_5km" in d for d in drivers)
    
    def test_fishing_zone_within_10km(self):
        severity, drivers = determine_socioeconomic_severity(0, 2)
        assert severity == "Medium"
        assert any("fishing_zone_within_10km" in d for d in drivers)
    
    def test_port_overrides_fishing(self):
        severity, drivers = determine_socioeconomic_severity(1, 3)
        assert severity == "High"
        assert any("port_within_5km" in d for d in drivers)


class TestEscalationRules:
    """Test rule-based escalation logic."""
    
    def test_no_escalation(self):
        severity, applied, reasons = apply_escalation_rules(
            "Low", "Low", "Low", 0, None
        )
        assert severity == "Low"
        assert applied is False
        assert reasons == []
    
    def test_breadth_escalation(self):
        # 2+ High/Critical receptors
        severity, applied, reasons = apply_escalation_rules(
            "Medium", "High", "Low", 2, None
        )
        assert severity == "High"
        assert applied is True
        assert any("multiple_high_ecological" in r for r in reasons)
    
    def test_cross_domain_escalation(self):
        # Both eco and socio are High+
        severity, applied, reasons = apply_escalation_rules(
            "High", "High", "High", 0, None
        )
        assert severity == "Critical"
        assert applied is True
        assert any("cross_domain" in r for r in reasons)
    
    def test_ttfe_escalation(self):
        # TTFE < 3 hours
        severity, applied, reasons = apply_escalation_rules(
            "Medium", "Medium", "Low", 0, 2.5
        )
        assert severity == "High"
        assert applied is True
        assert any("ttfe_urgency" in r for r in reasons)
    
    def test_max_one_tier_escalation(self):
        # Multiple escalation triggers, but capped at +1
        severity, applied, reasons = apply_escalation_rules(
            "Low", "High", "High", 3, 1.5
        )
        assert severity == "Medium"
        assert applied is True
        assert len(reasons) >= 2
    
    def test_critical_cannot_escalate(self):
        # Critical is max, cannot escalate further
        severity, applied, reasons = apply_escalation_rules(
            "Critical", "Critical", "High", 3, 1.0
        )
        assert severity == "Critical"


class TestSeverityScore:
    """Test 0-100 score calculation within fixed bands."""
    
    def test_none_severity_zero_score(self):
        score = calculate_severity_score("None", 0.5, 0.5, 0.5)
        assert score == 0.0
    
    def test_low_severity_band(self):
        # Low band: 0-24 (matches external LOW)
        score = calculate_severity_score("Low", 0.0, 0.0, 0.0)
        assert SCORE_BANDS["Low"][0] <= score <= SCORE_BANDS["Low"][1]
        assert score >= 0
        
        score = calculate_severity_score("Low", 1.0, 1.0, 1.0)
        assert SCORE_BANDS["Low"][0] <= score <= SCORE_BANDS["Low"][1]
        assert score <= 24
    
    def test_medium_severity_band(self):
        # Medium band: 25-49
        score = calculate_severity_score("Medium", 0.0, 0.0, 0.0)
        assert SCORE_BANDS["Medium"][0] <= score <= SCORE_BANDS["Medium"][1]
        
        score = calculate_severity_score("Medium", 1.0, 1.0, 1.0)
        assert SCORE_BANDS["Medium"][0] <= score <= SCORE_BANDS["Medium"][1]
    
    def test_high_severity_band(self):
        # High band: 50-74
        score = calculate_severity_score("High", 0.0, 0.0, 0.0)
        assert SCORE_BANDS["High"][0] <= score <= SCORE_BANDS["High"][1]
        
        score = calculate_severity_score("High", 1.0, 1.0, 1.0)
        assert SCORE_BANDS["High"][0] <= score <= SCORE_BANDS["High"][1]
    
    def test_critical_severity_band(self):
        # Critical band: 75-100
        score = calculate_severity_score("Critical", 0.0, 0.0, 0.0)
        assert SCORE_BANDS["Critical"][0] <= score <= SCORE_BANDS["Critical"][1]
        
        score = calculate_severity_score("Critical", 1.0, 1.0, 1.0)
        assert SCORE_BANDS["Critical"][0] <= score <= SCORE_BANDS["Critical"][1]
        assert score <= 100
    
    def test_intensity_increases_score(self):
        # Higher intensity should increase score within band
        low_score = calculate_severity_score("Medium", 0.2, 0.2, 0.2)
        high_score = calculate_severity_score("Medium", 0.8, 0.8, 0.8)
        assert high_score > low_score
    
    def test_score_never_overlaps_bands(self):
        # Ensure no overlap between bands
        max_low = calculate_severity_score("Low", 1.0, 1.0, 1.0)
        min_medium = calculate_severity_score("Medium", 0.0, 0.0, 0.0)
        assert max_low < min_medium
        
        max_medium = calculate_severity_score("Medium", 1.0, 1.0, 1.0)
        min_high = calculate_severity_score("High", 0.0, 0.0, 0.0)
        assert max_medium < min_high


class TestUrgencyFactor:
    """Test TTFE urgency factor calculation."""
    
    def test_no_ttfe(self):
        assert calculate_urgency_factor(None) == 0.0
        assert calculate_urgency_factor(0.0) == 0.0
        assert calculate_urgency_factor(-1.0) == 0.0
    
    def test_immediate_urgency(self):
        assert calculate_urgency_factor(0.5) == 1.0
    
    def test_high_urgency(self):
        assert calculate_urgency_factor(2.0) == 0.75
    
    def test_moderate_urgency(self):
        assert calculate_urgency_factor(4.0) == 0.5
    
    def test_low_urgency(self):
        assert calculate_urgency_factor(8.0) == 0.25
    
    def test_no_urgency(self):
        assert calculate_urgency_factor(12.0) == 0.0
        assert calculate_urgency_factor(24.0) == 0.0


class TestTrajectory:
    """Test trajectory determination."""
    
    def test_stable_trajectory(self):
        assert determine_trajectory("Medium", "Medium") == "STABLE"
        assert determine_trajectory("High", "High") == "STABLE"
    
    def test_worsening_trajectory(self):
        assert determine_trajectory("Low", "Medium") == "WORSENING"
        assert determine_trajectory("Medium", "High") == "WORSENING"
        assert determine_trajectory("High", "Critical") == "WORSENING"
    
    def test_improving_trajectory(self):
        assert determine_trajectory("Critical", "High") == "IMPROVING"
        assert determine_trajectory("High", "Medium") == "IMPROVING"
        assert determine_trajectory("Medium", "Low") == "IMPROVING"


class TestIntegrationScenarios:
    """Integration tests combining multiple functions."""
    
    def test_scenario_minor_spill_no_escalation(self):
        # Small spill, single Low receptor, no socioeconomic
        eco_impacts = [{"category": "Low", "receptor_type": "mangrove"}]
        
        eco_severity, high_crit, affected = determine_ecological_severity(eco_impacts)
        socio_severity, socio_drivers = determine_socioeconomic_severity(0, 0)
        
        assert eco_severity == "Low"
        assert socio_severity == "Low"
        
        base = max_severity([eco_severity, socio_severity])
        final, escalated, reasons = apply_escalation_rules(
            base, eco_severity, socio_severity, len(high_crit), None
        )
        
        assert final == "Low"
        assert escalated is False
        
        score = calculate_severity_score(final, 0.3, 0.25, 0.0)
        assert 1 <= score <= 24
    
    def test_scenario_moderate_with_breadth_escalation(self):
        # Multiple High receptors triggering breadth escalation
        eco_impacts = [
            {"category": "High", "receptor_type": "mangrove"},
            {"category": "High", "receptor_type": "coral_reef"},
            {"category": "Low", "receptor_type": "mpa"},
        ]
        
        eco_severity, high_crit, affected = determine_ecological_severity(eco_impacts)
        socio_severity, _ = determine_socioeconomic_severity(0, 1)
        
        assert eco_severity == "High"
        assert len(high_crit) == 2
        
        base = max_severity([eco_severity, socio_severity])
        final, escalated, reasons = apply_escalation_rules(
            base, eco_severity, socio_severity, len(high_crit), None
        )
        
        # High + breadth escalation = Critical
        assert final == "Critical"
        assert escalated is True
        assert any("multiple_high" in r for r in reasons)
    
    def test_scenario_cross_domain_with_urgency(self):
        # Both eco and socio High + TTFE < 3h
        eco_impacts = [
            {"category": "High", "receptor_type": "coral_reef"},
            {"category": "Medium", "receptor_type": "mangrove"},
        ]
        
        eco_severity, high_crit, affected = determine_ecological_severity(eco_impacts)
        socio_severity, _ = determine_socioeconomic_severity(2, 0)  # Ports nearby
        
        assert eco_severity == "High"
        assert socio_severity == "High"
        
        base = max_severity([eco_severity, socio_severity])
        final, escalated, reasons = apply_escalation_rules(
            base, eco_severity, socio_severity, len(high_crit), 2.0
        )
        
        # High + cross-domain + TTFE = Critical (capped at +1)
        assert final == "Critical"
        assert escalated is True
        assert len(reasons) >= 2
    
    def test_scenario_critical_with_multiple_triggers(self):
        # Critical base + multiple escalation triggers (should stay Critical)
        eco_impacts = [
            {"category": "Critical", "receptor_type": "coral_reef"},
            {"category": "High", "receptor_type": "mangrove"},
            {"category": "High", "receptor_type": "mpa"},
        ]
        
        eco_severity, high_crit, affected = determine_ecological_severity(eco_impacts)
        socio_severity, _ = determine_socioeconomic_severity(3, 2)
        
        base = max_severity([eco_severity, socio_severity])
        final, escalated, reasons = apply_escalation_rules(
            base, eco_severity, socio_severity, len(high_crit), 1.5
        )
        
        # Critical cannot escalate further
        assert final == "Critical"
        
        score = calculate_severity_score(final, 0.9, 0.75, 1.0)
        assert 75 <= score <= 100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
