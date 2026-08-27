import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "historical_evidence"


def test_seed_contains_four_real_indian_incidents_and_source_links():
    payload = json.loads((DATA / "seed_incidents.json").read_text())
    assert len(payload["incidents"]) == 4
    assert all(incident["country"] == "India" for incident in payload["incidents"])
    assert all(incident["is_synthetic"] is False for incident in payload["incidents"])
    source_keys = {source["id"] for source in payload["sources"]}
    source_aliases = {
        "src-tribune": "src-news-tribune",
        "src-deccanherald": "src-news-deccanherald",
    }
    assert all(
        all(source_aliases.get(source_id, source_id) in source_keys for source_id in incident["sources"])
        for incident in payload["incidents"]
    )


def test_seed_preserves_expected_incident_facts():
    payload = json.loads((DATA / "seed_incidents.json").read_text())
    by_title = {incident["title"]: incident for incident in payload["incidents"]}
    assert by_title["2010 Mumbai Harbour Oil Spill"]["latitude"] == 18.90
    assert by_title["2017 Ennore Oil Spill"]["incident_date"] == "2017-01-28"
    assert by_title["2021 Palghar/Vadrai Coast Oil Spill (Cyclone Tauktae aftermath)"]["volume_min_liters"] == 80000
    assert by_title["2023 Ennore Creek Oil Spill (Cyclone Michaung aftermath)"]["area_affected_km2"] == 20.0


def test_migration_defines_postgis_tables_and_idempotent_natural_keys():
    migration = (ROOT / "infra" / "postgres" / "migrations" / "002_historical_evidence.sql").read_text()
    assert "CREATE EXTENSION IF NOT EXISTS postgis" in migration
    assert "CREATE TABLE IF NOT EXISTS sources" in migration
    assert "CREATE TABLE IF NOT EXISTS incidents" in migration
    assert "CREATE TABLE IF NOT EXISTS incident_sources" in migration
    assert "GEOMETRY(Point, 4326)" in migration
    assert "UNIQUE (title, incident_date, country)" in migration
    assert "CREATE OR REPLACE VIEW v_incident_summary" in migration
    assert "ST_DWithin" not in migration


def test_seed_loader_uses_existing_database_environment_and_idempotent_upserts():
    loader = (DATA / "load_seed.py").read_text()
    assert "DATABASE_URL" in loader
    assert "POSTGRES_HOST" in loader
    assert "asyncpg" in loader
    assert "ON CONFLICT (name) DO UPDATE" in loader
    assert "ON CONFLICT (title, incident_date, country) DO UPDATE" in loader
    assert "ON CONFLICT (incident_id, source_id) DO NOTHING" in loader


def test_nearby_query_uses_geography_radius_and_deterministic_order():
    gateway = (ROOT / "services" / "api-gateway" / "app" / "main.py").read_text(encoding="utf-8")
    start = gateway.index('@app.get("/historical/incidents/near"')
    end = gateway.index('@app.get("/spill/incidents/{spill_id}"', start)
    query = gateway[start:end]
    assert "ST_DWithin" in query
    assert "geom::geography" in query
    assert "radius_km * 1000.0" in query
    assert "ORDER BY distance_km ASC, incident_date DESC, id ASC" in query
