import asyncio
import sys
from pathlib import Path

import asyncpg
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parents[1] / "AIS" / "services" / "api-gateway"))
from app import main as gateway


class CandidatePool:
    def __init__(self):
        self.calls = 0

    async def fetchrow(self, _query, _candidate_id):
        self.calls += 1
        if self.calls == 1:
            raise asyncpg.UndefinedColumnError("optical columns are absent")
        return {
            "candidate_id": _candidate_id,
            "scene_id": "scene",
            "acquisition_time": None,
            "lifecycle_status": "raw",
            "classification_label": None,
            "confidence": None,
            "area_m2": 10.0,
            "is_synthetic": False,
            "optical_oil_probability": None,
            "optical_predicted_class": None,
            "optical_cloud_free": None,
            "optical_checked_at": None,
            "geometry": None,
        }


class VesselPool:
    async def fetchrow(self, query, _value):
        if "attribution_results" in query:
            return {"spill_id": "00000000-0000-0000-0000-000000000001"}
        raise asyncpg.UndefinedTableError("optional table is absent")

    async def fetch(self, _query, _value):
        raise asyncpg.UndefinedTableError("optional table is absent")


def test_candidate_detail_degrades_when_optical_schema_is_missing(monkeypatch):
    pool = CandidatePool()
    monkeypatch.setattr(gateway, "_get_pool", lambda: _resolved(pool))

    async def no_incident(_candidate_id):
        raise HTTPException(status_code=404, detail="not promoted")

    monkeypatch.setattr(gateway, "get_spill_incident", no_incident)
    result = asyncio.run(gateway.get_spill_candidate("00000000-0000-0000-0000-000000000001"))

    assert result["status"] == "candidate"
    assert result["lifecycle_status"] == "raw"
    assert result["classification_label"] is None
    assert result["optical_oil_probability"] is None


def test_flagged_vessel_detail_degrades_when_optional_tables_are_missing(monkeypatch):
    pool = VesselPool()
    monkeypatch.setattr(gateway, "_get_pool", lambda: _resolved(pool))
    monkeypatch.setattr(
        gateway,
        "get_vessel_detail",
        lambda _mmsi: _resolved({
            "vessel": {"mmsi": "123"},
            "features": None,
            "risk": None,
            "anomalies": [],
            "trust": None,
            "sar_tasking": None,
            "verdict": {"status": "none", "spill_id": None},
        }),
    )
    monkeypatch.setattr(
        gateway,
        "get_spill_incident",
        lambda _spill_id: _resolved({"incident": None, "forecasts": []}),
    )
    result = asyncio.run(gateway.get_flagged_vessel_detail(123))

    assert result["linked_spill"]["incident"]["incident"] is None
    assert result["linked_spill"]["ecological"]["impact_count"] == 0
    assert result["linked_spill"]["cost"] is None
    assert result["flag_reasons"] == []


async def _resolved(value):
    return value
