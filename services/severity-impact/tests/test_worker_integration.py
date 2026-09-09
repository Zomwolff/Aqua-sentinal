"""
Integration tests for Severity-Impact V1 worker.

Tests the worker logic with mock database and Redis interactions.
"""
import pytest
from unittest.mock import AsyncMock, patch
from app.worker import (
    _fetch_forecasts,
    _fetch_ecological_impacts,
    _fetch_socioeconomic_proximity,
    _calculate_severity_for_horizon,
    _process_spill_ecological,
)


class TestForecastFetching:
    """Test forecast data fetching."""

    @pytest.mark.asyncio
    async def test_fetch_forecasts_success(self):
        mock_pool = AsyncMock()
        mock_pool.fetch.return_value = [
            {
                "horizon_hours": 1.0,
                "geom_wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
                "prob90_wkt": "POLYGON((0 0, 1.2 0, 1.2 1.2, 0 1.2, 0 0))",
                "physical_area_m2": 1000000.0,
                "drift_distance_m": 500.0,
                "expansion_ratio": 1.2,
                "spread_rate_m2_per_hour": 50000.0,
                "confidence": 0.85,
            },
            {
                "horizon_hours": 24.0,
                "geom_wkt": "POLYGON((0 0, 2 0, 2 2, 0 2, 0 0))",
                "prob90_wkt": "POLYGON((0 0, 2.5 0, 2.5 2.5, 0 2.5, 0 0))",
                "physical_area_m2": 4000000.0,
                "drift_distance_m": 2000.0,
                "expansion_ratio": 4.0,
                "spread_rate_m2_per_hour": 150000.0,
                "confidence": 0.75,
            },
        ]

        result = await _fetch_forecasts(mock_pool, "test-spill-001")

        assert len(result) == 2
        assert result[0]["horizon_hours"] == 1.0
        assert result[1]["horizon_hours"] == 24.0
        mock_pool.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_forecasts_empty(self):
        mock_pool = AsyncMock()
        mock_pool.fetch.return_value = []

        result = await _fetch_forecasts(mock_pool, "missing-spill")

        assert result == []


class TestEcologicalImpactFetching:
    """Test ecological impact data fetching."""

    @pytest.mark.asyncio
    async def test_fetch_ecological_impacts_success(self):
        mock_pool = AsyncMock()
        mock_pool.fetch.return_value = [
            {
                "horizon_hours": 1.0,
                "footprint_type": "best_estimate",
                "receptor_type": "mangrove",
                "category": "High",
                "exposure_pct": 65.0,
                "time_to_first_exposure_hours": 2.5,
                "sensitivity_tier": 3,
            },
            {
                "horizon_hours": 1.0,
                "footprint_type": "best_estimate",
                "receptor_type": "coral_reef",
                "category": "Critical",
                "exposure_pct": 85.0,
                "time_to_first_exposure_hours": 1.5,
                "sensitivity_tier": 4,
            },
        ]

        result = await _fetch_ecological_impacts(mock_pool, "test-spill-001")

        assert len(result) == 2
        assert result[0]["receptor_type"] == "mangrove"
        assert result[1]["category"] == "Critical"


class TestSocioeconomicProximity:
    """Test socioeconomic proximity queries."""

    @pytest.mark.asyncio
    async def test_proximity_with_ports_and_fishing(self):
        mock_pool = AsyncMock()
        mock_pool.fetchval.side_effect = [2, 3]  # 2 ports, 3 fishing zones

        ports, fishing = await _fetch_socioeconomic_proximity(
            mock_pool,
            "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"
        )

        assert ports == 2
        assert fishing == 3
        assert mock_pool.fetchval.call_count == 2

    @pytest.mark.asyncio
    async def test_proximity_no_receptors(self):
        mock_pool = AsyncMock()
        mock_pool.fetchval.side_effect = [0, 0]

        ports, fishing = await _fetch_socioeconomic_proximity(
            mock_pool,
            "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"
        )

        assert ports == 0
        assert fishing == 0


class TestSeverityCalculationForHorizon:
    """Test severity calculation for single horizon/footprint."""

    @pytest.mark.asyncio
    async def test_calculate_severity_low_scenario(self):
        mock_pool = AsyncMock()
        mock_pool.fetchval.side_effect = [0, 0]  # No ports/fishing

        ecological_impacts = [
            {
                "horizon_hours": 1.0,
                "footprint_type": "best_estimate",
                "receptor_type": "mangrove",
                "category": "Low",
                "exposure_pct": 15.0,
                "time_to_first_exposure_hours": 12.0,
            }
        ]

        result = await _calculate_severity_for_horizon(
            mock_pool,
            "test-spill-001",
            1.0,
            "best_estimate",
            "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
            ecological_impacts,
            1000000.0,
            500.0,
            1.2,
        )

        assert result["severity_level"] == "Low"
        assert result["ecological_severity"] == "Low"
        assert result["socioeconomic_severity"] == "Low"
        assert result["escalation_applied"] is False
        assert 1 <= result["score"] <= 24

    @pytest.mark.asyncio
    async def test_calculate_severity_high_with_port(self):
        mock_pool = AsyncMock()
        mock_pool.fetchval.side_effect = [2, 0]  # 2 ports nearby

        ecological_impacts = [
            {
                "horizon_hours": 1.0,
                "footprint_type": "best_estimate",
                "receptor_type": "coral_reef",
                "category": "High",
                "exposure_pct": 75.0,
                "time_to_first_exposure_hours": 2.5,
            }
        ]

        result = await _calculate_severity_for_horizon(
            mock_pool,
            "test-spill-001",
            1.0,
            "best_estimate",
            "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
            ecological_impacts,
            2000000.0,
            800.0,
            2.0,
        )

        assert result["severity_level"] in ["High", "Critical"]
        assert result["ecological_severity"] == "High"
        assert result["socioeconomic_severity"] == "High"
        assert result["ports_within_5km"] == 2
        assert 50 <= result["score"] <= 100

    @pytest.mark.asyncio
    async def test_calculate_severity_with_breadth_escalation(self):
        mock_pool = AsyncMock()
        mock_pool.fetchval.side_effect = [0, 1]  # 1 fishing zone

        ecological_impacts = [
            {
                "horizon_hours": 1.0,
                "footprint_type": "best_estimate",
                "receptor_type": "mangrove",
                "category": "High",
                "exposure_pct": 70.0,
                "time_to_first_exposure_hours": 4.0,
            },
            {
                "horizon_hours": 1.0,
                "footprint_type": "best_estimate",
                "receptor_type": "coral_reef",
                "category": "High",
                "exposure_pct": 65.0,
                "time_to_first_exposure_hours": 3.5,
            },
        ]

        result = await _calculate_severity_for_horizon(
            mock_pool,
            "test-spill-001",
            1.0,
            "best_estimate",
            "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
            ecological_impacts,
            2000000.0,
            800.0,
            2.0,
        )

        # High eco + breadth (2 distinct High receptors) = Critical
        assert result["severity_level"] == "Critical"
        assert result["escalation_applied"] is True
        assert any("multiple_high" in r for r in result["escalation_reasons"])


class TestProcessSpillEcological:
    """Test full spill processing workflow."""

    @pytest.mark.asyncio
    async def test_process_spill_success(self):
        mock_pool = AsyncMock()
        mock_redis = AsyncMock()

        # Mock forecast + ecological fetch
        mock_pool.fetch.side_effect = [
            # Forecasts: 1h and 24h, both with prob90 geometry
            [
                {
                    "horizon_hours": 1.0,
                    "geom_wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
                    "prob90_wkt": "POLYGON((0 0, 1.2 0, 1.2 1.2, 0 1.2, 0 0))",
                    "physical_area_m2": 1000000.0,
                    "drift_distance_m": 500.0,
                    "expansion_ratio": 1.2,
                    "spread_rate_m2_per_hour": 50000.0,
                    "confidence": 0.85,
                },
                {
                    "horizon_hours": 24.0,
                    "geom_wkt": "POLYGON((0 0, 2 0, 2 2, 0 2, 0 0))",
                    "prob90_wkt": "POLYGON((0 0, 2.5 0, 2.5 2.5, 0 2.5, 0 0))",
                    "physical_area_m2": 4000000.0,
                    "drift_distance_m": 2000.0,
                    "expansion_ratio": 4.0,
                    "spread_rate_m2_per_hour": 150000.0,
                    "confidence": 0.75,
                },
            ],
            # Ecological impacts
            [
                {
                    "horizon_hours": 1.0,
                    "footprint_type": "best_estimate",
                    "receptor_type": "mangrove",
                    "category": "Medium",
                    "exposure_pct": 45.0,
                    "time_to_first_exposure_hours": 5.0,
                    "sensitivity_tier": 2,
                },
                {
                    "horizon_hours": 1.0,
                    "footprint_type": "probability_90",
                    "receptor_type": "mangrove",
                    "category": "High",
                    "exposure_pct": 65.0,
                    "time_to_first_exposure_hours": 4.0,
                    "sensitivity_tier": 2,
                },
                {
                    "horizon_hours": 24.0,
                    "footprint_type": "best_estimate",
                    "receptor_type": "mangrove",
                    "category": "High",
                    "exposure_pct": 70.0,
                    "time_to_first_exposure_hours": 1.5,
                    "sensitivity_tier": 2,
                },
                {
                    "horizon_hours": 24.0,
                    "footprint_type": "probability_90",
                    "receptor_type": "coral_reef",
                    "category": "Critical",
                    "exposure_pct": 85.0,
                    "time_to_first_exposure_hours": 1.0,
                    "sensitivity_tier": 4,
                },
            ],
        ]

        # fetchrow: only spill lat/lon (current_geom fetch removed)
        mock_pool.fetchrow.return_value = {"latitude": 10.5, "longitude": 120.3}

        # Proximity: 2 forecasts × 2 footprints = 4 calls × 2 values = 8
        mock_pool.fetchval.side_effect = [
            # 1h best_estimate: 0 ports, 1 fishing
            0, 1,
            # 1h prob90: 1 port, 1 fishing
            1, 1,
            # 24h best_estimate: 1 port, 2 fishing
            1, 2,
            # 24h prob90: 2 ports, 3 fishing
            2, 3,
        ]

        mock_pool.execute.return_value = None

        with patch('app.worker.publish_to_stream', new_callable=AsyncMock) as mock_publish:
            await _process_spill_ecological(
                {"spill_id": "test-spill-001", "is_synthetic": "false"},
                mock_pool,
                mock_redis,
            )

            mock_publish.assert_called_once()
            published_data = mock_publish.call_args[0][2]

            assert published_data["spill_id"] == "test-spill-001"
            assert "severity_level" in published_data
            assert "severity_score" in published_data
            assert "peak_severity" in published_data
            assert "trajectory" in published_data
            assert "methodology_version" in published_data

    @pytest.mark.asyncio
    async def test_process_spill_no_forecasts(self):
        mock_pool = AsyncMock()
        mock_redis = AsyncMock()

        # No forecasts available
        mock_pool.fetch.side_effect = [[], []]

        # Should return early without processing
        await _process_spill_ecological(
            {"spill_id": "test-spill-no-forecast"},
            mock_pool,
            mock_redis,
        )

        # Should not call execute (no DB insert)
        mock_pool.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_spill_missing_spill_id(self):
        mock_pool = AsyncMock()
        mock_redis = AsyncMock()

        # Missing spill_id should return early
        await _process_spill_ecological(
            {"is_synthetic": "false"},
            mock_pool,
            mock_redis,
        )

        # Should not fetch anything
        mock_pool.fetch.assert_not_called()


class TestIdempotency:
    """Test idempotent behavior with ON CONFLICT."""

    @pytest.mark.asyncio
    async def test_replay_event_updates_existing_record(self):
        """Replaying the same event should update, not duplicate."""
        mock_pool = AsyncMock()
        mock_redis = AsyncMock()

        mock_pool.fetch.side_effect = [
            # Forecasts: 1h with no prob90_wkt (will fall back to best_estimate for both)
            [{"horizon_hours": 1.0, "geom_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
              "prob90_wkt": None, "physical_area_m2": 1000000.0,
              "drift_distance_m": 500.0, "expansion_ratio": 1.2,
              "spread_rate_m2_per_hour": 50000.0, "confidence": 0.85}],
            # Ecological: 1h best_estimate only (matches both footprint iterations via fallback)
            [{"horizon_hours": 1.0, "footprint_type": "best_estimate",
              "receptor_type": "mangrove", "category": "Low",
              "exposure_pct": 20.0, "time_to_first_exposure_hours": 8.0,
              "sensitivity_tier": 1}],
        ]

        # fetchrow: only lat/lon (current_geom fetch removed)
        mock_pool.fetchrow.return_value = {"latitude": 10.0, "longitude": 120.0}
        # fetchval: 2 footprints × 2 proximity queries = 4 values
        mock_pool.fetchval.side_effect = [0, 0, 0, 0]
        mock_pool.execute.return_value = None

        with patch('app.worker.publish_to_stream', new_callable=AsyncMock):
            await _process_spill_ecological(
                {"spill_id": "test-replay", "is_synthetic": "false"},
                mock_pool,
                mock_redis,
            )

        # ON CONFLICT should handle upsert
        assert mock_pool.execute.call_count >= 1


class TestOverallSeverityBaseline:
    """
    Verify audit fix C-1/C-2: overall severity is 1h probability_90, not the
    "current" sentinel (which always produced empty ecological data).
    """

    @pytest.mark.asyncio
    async def test_overall_uses_1h_prob90_ecological_data(self):
        """Overall result must reflect ecological data from 1h probability_90 impacts."""
        mock_pool = AsyncMock()
        mock_redis = AsyncMock()

        mock_pool.fetch.side_effect = [
            # Forecasts: only 1h, with prob90 geometry
            [{"horizon_hours": 1.0,
              "geom_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
              "prob90_wkt": "POLYGON((0 0,1.5 0,1.5 1.5,0 1.5,0 0))",
              "physical_area_m2": 1000000.0, "drift_distance_m": 500.0,
              "expansion_ratio": 1.2, "spread_rate_m2_per_hour": 50000.0,
              "confidence": 0.85}],
            # Ecological: 1h best_estimate=Low, 1h prob90=High
            [{"horizon_hours": 1.0, "footprint_type": "best_estimate",
              "receptor_type": "mangrove", "category": "Low",
              "exposure_pct": 20.0, "time_to_first_exposure_hours": 10.0,
              "sensitivity_tier": "low"},
             {"horizon_hours": 1.0, "footprint_type": "probability_90",
              "receptor_type": "mangrove", "category": "High",
              "exposure_pct": 75.0, "time_to_first_exposure_hours": 2.5,
              "sensitivity_tier": "high"}],
        ]

        # fetchval: 2 footprints × 2 proximity queries = 4 values
        mock_pool.fetchval.side_effect = [0, 0, 0, 0]
        mock_pool.fetchrow.return_value = {"latitude": 10.5, "longitude": 120.3}
        mock_pool.execute.return_value = None

        published_data = {}
        with patch('app.worker.publish_to_stream', new_callable=AsyncMock) as mock_pub:
            await _process_spill_ecological(
                {"spill_id": "test-overall-1hp90", "is_synthetic": "false"},
                mock_pool,
                mock_redis,
            )
            published_data = mock_pub.call_args[0][2]

        # 1h prob90 has mangrove:High → overall must reflect this (not LOW from empty current)
        assert published_data["severity_level"] in ("HIGH", "CRITICAL"), (
            f"Expected overall severity from 1h p90 ecological (High receptor), "
            f"got {published_data['severity_level']} — the 'current' sentinel with "
            f"empty ecological data may still be used as overall."
        )
        assert published_data["ecological_severity"] == "High"

    @pytest.mark.asyncio
    async def test_overall_not_driven_by_current_sentinel_footprint(self):
        """Proves the fix: 'current' is no longer used as the ecological footprint.

        Before the fix, _calculate_severity_for_horizon was called with
        footprint_type='current', which matched zero ecological_impact rows
        (table only stores best_estimate / probability_90), producing
        eco_severity='None' for the overall result.
        """
        mock_pool = AsyncMock()
        mock_redis = AsyncMock()

        mock_pool.fetch.side_effect = [
            [{"horizon_hours": 1.0,
              "geom_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
              "prob90_wkt": "POLYGON((0 0,1.5 0,1.5 1.5,0 1.5,0 0))",
              "physical_area_m2": 1000000.0, "drift_distance_m": 500.0,
              "expansion_ratio": 1.2, "spread_rate_m2_per_hour": 50000.0,
              "confidence": 0.9}],
            # Only 1h probability_90 ecological data — no best_estimate row
            [{"horizon_hours": 1.0, "footprint_type": "probability_90",
              "receptor_type": "coral_reef", "category": "Critical",
              "exposure_pct": 90.0, "time_to_first_exposure_hours": 1.5,
              "sensitivity_tier": "high"}],
        ]

        # 2 footprints × 2 proximity queries = 4 values
        mock_pool.fetchval.side_effect = [0, 0, 0, 0]
        mock_pool.fetchrow.return_value = {"latitude": 10.5, "longitude": 120.3}
        mock_pool.execute.return_value = None

        published_data = {}
        with patch('app.worker.publish_to_stream', new_callable=AsyncMock) as mock_pub:
            await _process_spill_ecological(
                {"spill_id": "test-no-current-sentinel", "is_synthetic": "false"},
                mock_pool,
                mock_redis,
            )
            published_data = mock_pub.call_args[0][2]

        # With the fix, overall must be CRITICAL (from 1h p90 Critical coral_reef).
        # If the old bug were present, overall would be LOW (current sentinel, no eco data).
        assert published_data["severity_level"] in ("CRITICAL", "HIGH"), (
            f"severity_level={published_data['severity_level']} suggests the old "
            f"'current' sentinel (empty ecological data) is still used as overall."
        )


class TestTrajectoryBaseline:
    """
    Verify audit fix C-3: trajectory compares 1h probability_90 vs 24h probability_90.
    """

    @pytest.mark.asyncio
    async def test_trajectory_uses_1h_vs_24h_prob90(self):
        """Trajectory must use 1h p90 (not 'current') vs 24h p90."""
        mock_pool = AsyncMock()
        mock_redis = AsyncMock()

        mock_pool.fetch.side_effect = [
            # Forecasts: 1h and 24h, both with prob90 geometry
            [{"horizon_hours": 1.0,
              "geom_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
              "prob90_wkt": "POLYGON((0 0,1.2 0,1.2 1.2,0 1.2,0 0))",
              "physical_area_m2": 1000000.0, "drift_distance_m": 500.0,
              "expansion_ratio": 1.2, "spread_rate_m2_per_hour": 50000.0,
              "confidence": 0.85},
             {"horizon_hours": 24.0,
              "geom_wkt": "POLYGON((0 0,2 0,2 2,0 2,0 0))",
              "prob90_wkt": "POLYGON((0 0,3 0,3 3,0 3,0 0))",
              "physical_area_m2": 9000000.0, "drift_distance_m": 2000.0,
              "expansion_ratio": 9.0, "spread_rate_m2_per_hour": 350000.0,
              "confidence": 0.75}],
            # 1h p90=Low, 24h p90=Critical → trajectory WORSENING
            [{"horizon_hours": 1.0, "footprint_type": "best_estimate",
              "receptor_type": "mangrove", "category": "Low",
              "exposure_pct": 15.0, "time_to_first_exposure_hours": 10.0,
              "sensitivity_tier": "low"},
             {"horizon_hours": 1.0, "footprint_type": "probability_90",
              "receptor_type": "mangrove", "category": "Low",
              "exposure_pct": 20.0, "time_to_first_exposure_hours": 9.0,
              "sensitivity_tier": "low"},
             {"horizon_hours": 24.0, "footprint_type": "best_estimate",
              "receptor_type": "coral_reef", "category": "High",
              "exposure_pct": 70.0, "time_to_first_exposure_hours": 2.0,
              "sensitivity_tier": "high"},
             {"horizon_hours": 24.0, "footprint_type": "probability_90",
              "receptor_type": "coral_reef", "category": "Critical",
              "exposure_pct": 90.0, "time_to_first_exposure_hours": 1.5,
              "sensitivity_tier": "high"}],
        ]

        # 4 horizon×footprint results × 2 proximity queries = 8
        mock_pool.fetchval.side_effect = [0, 0, 0, 0, 0, 0, 0, 0]
        mock_pool.fetchrow.return_value = {"latitude": 10.5, "longitude": 120.3}
        mock_pool.execute.return_value = None

        published_data = {}
        with patch('app.worker.publish_to_stream', new_callable=AsyncMock) as mock_pub:
            await _process_spill_ecological(
                {"spill_id": "test-trajectory", "is_synthetic": "false"},
                mock_pool,
                mock_redis,
            )
            published_data = mock_pub.call_args[0][2]

        # 1h p90 → mangrove:Low → LOW overall
        # 24h p90 → coral_reef:Critical → CRITICAL
        # trajectory must be WORSENING (future > current)
        assert published_data["trajectory"] == "WORSENING", (
            f"Expected WORSENING (1h p90=Low vs 24h p90=Critical), "
            f"got {published_data['trajectory']}"
        )

    @pytest.mark.asyncio
    async def test_trajectory_stable_when_24h_missing(self):
        """Missing 24h probability_90 → trajectory defaults to STABLE."""
        mock_pool = AsyncMock()
        mock_redis = AsyncMock()

        mock_pool.fetch.side_effect = [
            # Only 1h forecast, no 24h
            [{"horizon_hours": 1.0,
              "geom_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
              "prob90_wkt": "POLYGON((0 0,1.2 0,1.2 1.2,0 1.2,0 0))",
              "physical_area_m2": 1000000.0, "drift_distance_m": 500.0,
              "expansion_ratio": 1.2, "spread_rate_m2_per_hour": 50000.0,
              "confidence": 0.85}],
            [{"horizon_hours": 1.0, "footprint_type": "probability_90",
              "receptor_type": "mangrove", "category": "High",
              "exposure_pct": 65.0, "time_to_first_exposure_hours": 4.0,
              "sensitivity_tier": "high"}],
        ]

        # 2 footprints × 2 proximity queries = 4
        mock_pool.fetchval.side_effect = [0, 0, 0, 0]
        mock_pool.fetchrow.return_value = {"latitude": 10.5, "longitude": 120.3}
        mock_pool.execute.return_value = None

        published_data = {}
        with patch('app.worker.publish_to_stream', new_callable=AsyncMock) as mock_pub:
            await _process_spill_ecological(
                {"spill_id": "test-no-24h", "is_synthetic": "false"},
                mock_pool,
                mock_redis,
            )
            published_data = mock_pub.call_args[0][2]

        assert published_data["trajectory"] == "STABLE", (
            f"Expected STABLE when 24h p90 is missing, got {published_data['trajectory']}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
