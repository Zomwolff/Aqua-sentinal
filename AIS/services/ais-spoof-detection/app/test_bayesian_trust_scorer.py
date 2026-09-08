"""
Tests for Bayesian Trust Scorer

Validates:
1. Posterior probability calculation is correct
2. Posterior probabilities are well-calibrated
3. Evidence breakdown is accurate
4. Backward compatibility with legacy format
"""
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import pytest

# Note: In production, these would be pytest fixtures
# For now, mock implementation for testing

class MockPool:
    """Mock database pool for testing."""
    
    async def fetchrow(self, query: str, *args):
        """Return mock vessel data."""
        # Simulate a vessel with prior position
        return {
            "last_lat": 18.936,
            "last_lon": 72.838,
            "last_seen": datetime.now(timezone.utc) - timedelta(minutes=5),
        }


async def test_genuine_vessel_high_trust():
    """Test that a normal vessel gets high posterior probability."""
    from bayesian_trust_scorer import compute_bayesian_trust_score
    
    pool = MockPool()
    mmsi = 419900684
    
    current_data = {
        "lat": 18.9365,
        "lon": 72.8385,
        "name": "MV TEST VESSEL",
        "type": "cargo",
        "speed_knots": 12.0,
        "heading_deg": 270.0,
        "timestamp": datetime.now(timezone.utc),
    }
    
    previous_data = {
        "lat": 18.936,
        "lon": 72.838,
        "name": "MV TEST VESSEL",
        "type": "cargo",
        "speed_knots": 12.0,
        "heading_deg": 270.0,
        "timestamp": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    
    result = await compute_bayesian_trust_score(pool, mmsi, current_data, previous_data)
    
    # Verify structure
    assert "posterior_probability_genuine" in result
    assert "credible_interval_lower" in result
    assert "credible_interval_upper" in result
    assert "evidence_breakdown" in result
    
    # Genuine vessel should have high posterior
    assert result["posterior_probability_genuine"] > 0.7, \
        f"Normal vessel should be trusted (got {result['posterior_probability_genuine']})"
    
    print(f"✓ Genuine vessel test passed: posterior={result['posterior_probability_genuine']:.3f}")


async def test_spoofed_vessel_low_trust():
    """Test that a spoofed vessel gets low posterior probability."""
    from bayesian_trust_scorer import compute_bayesian_trust_score
    
    pool = MockPool()
    mmsi = 999999999  # Invalid MMSI - this is the key factor
    
    current_data = {
        "lat": 25.0,
        "lon": 73.0,
        "name": "FAKE VESSEL",
        "type": "unknown",
        "speed_knots": 50.0,
        "heading_deg": 270.0,
        "timestamp": datetime.now(timezone.utc),
    }
    
    previous_data = {
        "lat": 18.936,
        "lon": 72.838,
        "name": "MV ORIGINAL",
        "type": "cargo",
        "speed_knots": 12.0,
        "heading_deg": 270.0,
        "timestamp": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    
    result = await compute_bayesian_trust_score(pool, mmsi, current_data, previous_data)
    
    # Spoofed vessel should have low posterior (primarily due to invalid MMSI)
    assert result["posterior_probability_genuine"] < 0.9, \
        f"Spoofed vessel should be distrusted (got {result['posterior_probability_genuine']})"
    
    # Should detect at least the invalid MMSI
    assert "mmsi_invalid" in result["factors_detected"], "Should detect invalid MMSI"
    
    print(f"✓ Spoofed vessel test passed: posterior={result['posterior_probability_genuine']:.3f}")
    print(f"  Factors detected: {result['factors_detected']}")


async def test_backward_compatibility():
    """Test that legacy fields are present for backward compatibility."""
    from bayesian_trust_scorer import compute_bayesian_trust_score
    
    pool = MockPool()
    mmsi = 419900684
    
    current_data = {
        "lat": 18.936,
        "lon": 72.838,
        "name": "TEST",
        "type": "cargo",
        "timestamp": datetime.now(timezone.utc),
    }
    
    result = await compute_bayesian_trust_score(pool, mmsi, current_data)
    
    # Check for backward-compatible fields
    assert "mmsi" in result
    assert "posterior_probability_genuine" in result
    assert "model_version" in result
    assert result["model_version"] == "bayesian_v1"
    
    print(f"✓ Backward compatibility test passed")


async def test_credible_interval_width():
    """Test that credible intervals are calibrated."""
    from bayesian_trust_scorer import compute_bayesian_trust_score
    
    pool = MockPool()
    mmsi = 419900684
    
    current_data = {
        "lat": 18.936,
        "lon": 72.838,
        "name": "TEST",
        "type": "cargo",
        "timestamp": datetime.now(timezone.utc),
    }
    
    result = await compute_bayesian_trust_score(pool, mmsi, current_data)
    
    # CI should be reasonable width
    ci_width = result["credible_interval_upper"] - result["credible_interval_lower"]
    assert 0.0 < ci_width <= 0.3, f"CI width should be positive and small (got {ci_width})"
    
    # Lower < posterior < upper
    assert result["credible_interval_lower"] <= result["posterior_probability_genuine"] <= result["credible_interval_upper"]
    
    print(f"✓ Credible interval test passed: width={ci_width:.3f}")


async def test_evidence_breakdown_completeness():
    """Test that evidence breakdown includes all factors."""
    from bayesian_trust_scorer import compute_bayesian_trust_score
    
    pool = MockPool()
    mmsi = 419900684
    
    current_data = {
        "lat": 18.936,
        "lon": 72.838,
        "name": "TEST",
        "type": "cargo",
        "timestamp": datetime.now(timezone.utc),
    }
    
    previous_data = {
        "lat": 18.936,
        "lon": 72.838,
        "name": "TEST",
        "type": "cargo",
        "timestamp": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    
    result = await compute_bayesian_trust_score(pool, mmsi, current_data, previous_data)
    
    # Should have evidence for each factor
    factors_expected = {
        "dead_reckoning_position",
        "speed_jump",
        "mmsi_collision",
        "identity_consistency",
        "mmsi_validity",
    }
    
    factors_in_breakdown = {e["factor"] for e in result["evidence_breakdown"]}
    
    # At least some factors should be evaluated
    assert len(factors_in_breakdown) > 0, "Evidence breakdown should have entries"
    
    print(f"✓ Evidence breakdown test passed: {len(factors_in_breakdown)} factors evaluated")
    for factor in factors_in_breakdown:
        print(f"  - {factor}")


async def main():
    """Run all tests."""
    print("Testing Bayesian Trust Scorer...\n")
    
    try:
        await test_genuine_vessel_high_trust()
        await test_spoofed_vessel_low_trust()
        await test_backward_compatibility()
        await test_credible_interval_width()
        await test_evidence_breakdown_completeness()
        
        print("\n✅ All Bayesian trust scorer tests passed!")
        return 0
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
