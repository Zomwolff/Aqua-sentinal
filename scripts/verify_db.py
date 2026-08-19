import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.db.connection import close_pool, create_pool

EXPECTED_TABLES = [
    "vessels",
    "vessel_positions",
    "spill_incidents",
    "attribution_results",
    "forecasts",
    "severity",
    "response_recommendations",
    "protected_areas",
    "environmental_conditions",
]

EXPECTED_COLUMNS = {
    "vessels": {
        "id": ("bigint", 0),
        "imo_number": ("character varying", 20),
        "mmsi": ("character varying", 20),
        "name": ("character varying", 0),
        "vessel_type": ("text", 0),
        "flag": ("character varying", 0),
        "length_m": ("numeric", 0),
        "width_m": ("numeric", 0),
        "gross_tonnage": ("numeric", 0),
        "operator": ("character varying", 0),
        "created_at": ("timestamp with time zone", 0),
        "updated_at": ("timestamp with time zone", 0),
    },
    "vessel_positions": {
        "id": ("bigint", 0),
        "vessel_id": ("bigint", 0),
        "timestamp": ("timestamp with time zone", 0),
        "latitude": ("double precision", 0),
        "longitude": ("double precision", 0),
        "speed_knots": ("numeric", 0),
        "course_deg": ("numeric", 0),
        "heading_deg": ("numeric", 0),
        "nav_status": ("character varying", 0),
        "source": ("character varying", 0),
    },
    "spill_incidents": {
        "id": ("uuid", 0),
        "detected_at": ("timestamp with time zone", 0),
        "latitude": ("double precision", 0),
        "longitude": ("double precision", 0),
        "area_km2": ("numeric", 0),
        "confidence": ("numeric", 0),
        "source": ("text", 0),
        "source_image_id": ("character varying", 0),
        "status": ("text", 0),
    },
    "attribution_results": {
        "id": ("bigint", 0),
        "spill_id": ("uuid", 0),
        "vessel_id": ("bigint", 0),
        "distance_score": ("numeric", 0),
        "trajectory_score": ("numeric", 0),
        "wind_score": ("numeric", 0),
        "time_score": ("numeric", 0),
        "behavior_score": ("numeric", 0),
        "final_score": ("numeric", 0),
        "model_version": ("character varying", 0),
        "computed_at": ("timestamp with time zone", 0),
    },
    "forecasts": {
        "id": ("bigint", 0),
        "spill_id": ("uuid", 0),
        "forecast_time": ("timestamp with time zone", 0),
        "generated_at": ("timestamp with time zone", 0),
        "horizon_hours": ("numeric", 0),
        "model_version": ("character varying", 0),
        "confidence": ("numeric", 0),
    },
    "severity": {
        "id": ("bigint", 0),
        "spill_id": ("uuid", 0),
        "severity_level": ("text", 0),
        "score": ("numeric", 0),
        "environmental_risk": ("numeric", 0),
        "population_risk": ("numeric", 0),
        "economic_risk": ("numeric", 0),
        "protected_area_risk": ("numeric", 0),
        "computed_at": ("timestamp with time zone", 0),
    },
    "response_recommendations": {
        "id": ("bigint", 0),
        "spill_id": ("uuid", 0),
        "recommendation": ("text", 0),
        "priority": ("text", 0),
        "status": ("text", 0),
        "generated_at": ("timestamp with time zone", 0),
        "acknowledged_at": ("timestamp with time zone", 0),
        "acknowledged_by": ("character varying", 0),
    },
    "protected_areas": {
        "id": ("bigint", 0),
        "name": ("character varying", 0),
        "area_type": ("character varying", 0),
        "metadata": ("jsonb", 0),
    },
    "environmental_conditions": {
        "id": ("bigint", 0),
        "timestamp": ("timestamp with time zone", 0),
        "latitude": ("double precision", 0),
        "longitude": ("double precision", 0),
        "wind_speed_kmh": ("numeric", 0),
        "wind_direction_deg": ("numeric", 0),
        "current_speed_ms": ("numeric", 0),
        "current_direction_deg": ("numeric", 0),
        "source": ("character varying", 0),
    },
}

EXPECTED_SPATIAL = {
    ("vessel_positions", "geom"): ("POINT", 4326),
    ("spill_incidents", "geom"): ("POLYGON", 4326),
    ("spill_incidents", "centroid"): ("POINT", 4326),
    ("forecasts", "geom"): ("POLYGON", 4326),
    ("protected_areas", "geom"): ("MULTIPOLYGON", 4326),
    ("environmental_conditions", "geom"): ("POINT", 4326),
}

EXPECTED_FKS = {
    ("vessel_positions", "vessel_id", "vessels", "id"),
    ("attribution_results", "spill_id", "spill_incidents", "id"),
    ("attribution_results", "vessel_id", "vessels", "id"),
    ("forecasts", "spill_id", "spill_incidents", "id"),
    ("severity", "spill_id", "spill_incidents", "id"),
    ("response_recommendations", "spill_id", "spill_incidents", "id"),
}

EXPECTED_INDEXES = {
    "idx_vessel_positions_geom": "gist",
    "idx_spill_incidents_geom": "gist",
    "idx_spill_incidents_centroid": "gist",
    "idx_forecasts_geom": "gist",
    "idx_protected_areas_geom": "gist",
    "idx_environmental_conditions_geom": "gist",
    "idx_vessel_positions_vessel_id": "btree",
    "idx_vessel_positions_timestamp": "btree",
    "idx_attribution_results_spill_id": "btree",
    "idx_attribution_results_vessel_id": "btree",
    "idx_forecasts_spill_id": "btree",
    "idx_severity_spill_id": "btree",
    "idx_response_recommendations_spill_id": "btree",
    "idx_environmental_conditions_timestamp": "btree",
}

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))


async def run() -> int:
    try:
        pool = await create_pool()
    except Exception as exc:
        print("ERROR: could not connect to PostgreSQL:", exc)
        return 1

    try:
        conn = await pool.acquire()
        try:
            await conn.fetchval("SELECT 1")
            check("PostgreSQL", True, "connection OK")
            pgis = await conn.fetchval("SELECT PostGIS_Version()")
            check("PostGIS", bool(pgis), pgis or "PostGIS unavailable")

            rows = await conn.fetch(
                """
                SELECT table_name, column_name, data_type,
                       COALESCE(character_maximum_length, 0) AS char_len,
                       is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                """
            )
            columns = {}
            for r in rows:
                columns[(r["table_name"], r["column_name"])] = (
                    r["data_type"],
                    r["char_len"],
                    r["is_nullable"],
                )

            existing_tables = {r["table_name"] for r in rows}
            for t in EXPECTED_TABLES:
                check(f"table {t}", t in existing_tables, "found" if t in existing_tables else "MISSING")

            for t, cols in EXPECTED_COLUMNS.items():
                for c, (dtype, clen) in cols.items():
                    got = columns.get((t, c))
                    ok = got is not None and got[0] == dtype and (clen == 0 or got[1] == clen)
                    detail = (
                        "OK"
                        if ok
                        else f"expected {dtype}"
                        + (f"({clen})" if clen else "")
                        + (f" got {got[0]}" + (f"({got[1]})" if got and got[1] else "") if got else " MISSING")
                    )
                    check(f"{t}.{c}", ok, detail)

            check(
                "vessels.mmsi NOT NULL",
                columns.get(("vessels", "mmsi"), (None, None, "YES"))[2] == "NO",
            )

            pk_rows = await conn.fetch(
                """
                SELECT conrelid::regclass::text AS tbl, a.attname AS col
                FROM pg_constraint c
                JOIN LATERAL unnest(c.conkey) AS k(attnum) ON TRUE
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                WHERE c.contype = 'p'
                """
            )
            pks = {(r["tbl"], r["col"]) for r in pk_rows}
            for t in EXPECTED_TABLES:
                check(f"PK {t}.id", (t, "id") in pks, "OK" if (t, "id") in pks else "MISSING")

            fk_rows = await conn.fetch(
                """
                SELECT conrelid::regclass::text AS src_table,
                       a.attname AS src_col,
                       confrelid::regclass::text AS ref_table,
                       b.attname AS ref_col
                FROM pg_constraint c
                JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                JOIN LATERAL unnest(c.confkey) WITH ORDINALITY AS fk(attnum, ord) ON TRUE
                JOIN pg_attribute b ON b.attrelid = c.confrelid AND b.attnum = fk.attnum
                WHERE c.contype = 'f' AND k.ord = fk.ord
                """
            )
            fks = {(r["src_table"], r["src_col"], r["ref_table"], r["ref_col"]) for r in fk_rows}
            for fk in EXPECTED_FKS:
                check("FK " + ".".join(fk[:2]) + " -> " + ".".join(fk[2:]), fk in fks, "OK" if fk in fks else "MISSING")

            spatial_rows = await conn.fetch(
                """
                SELECT f_table_name, f_geometry_column, type, srid
                FROM geometry_columns
                WHERE f_table_schema = 'public'
                """
            )
            spatial = {(r["f_table_name"], r["f_geometry_column"]): (r["type"], r["srid"]) for r in spatial_rows}
            for (t, c), (gtype, srid) in EXPECTED_SPATIAL.items():
                got = spatial.get((t, c))
                ok = got is not None and got[0] == gtype and got[1] == srid
                detail = "OK" if ok else (f"expected geometry({gtype},{srid}) actual {got}" if got else "MISSING")
                check(f"spatial {t}.{c}", ok, detail)

            idx_rows = await conn.fetch(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
            )
            indexes = {r["indexname"]: r["indexdef"] for r in idx_rows}
            for name, kind in EXPECTED_INDEXES.items():
                got = indexes.get(name)
                if kind == "gist":
                    ok = got is not None and "USING gist" in got
                else:
                    ok = got is not None
                detail = "OK" if ok else "MISSING"
                check(f"index {name}", ok, detail)

            unique_rows = await conn.fetch(
                """
                SELECT t.relname AS tbl, a.attname AS col
                FROM pg_index ix
                JOIN pg_class t ON t.oid = ix.indrelid
                JOIN LATERAL unnest(ix.indkey) AS k(attnum) ON TRUE
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                WHERE ix.indisunique AND t.relname IN ('vessels')
                """
            )
            unique_cols = {r["col"] for r in unique_rows if r["tbl"] == "vessels"}
            check("vessels.mmsi UNIQUE", "mmsi" in unique_cols, "OK" if "mmsi" in unique_cols else "MISSING")
            check("vessels.imo_number UNIQUE", "imo_number" in unique_cols, "OK" if "imo_number" in unique_cols else "MISSING")
        finally:
            await pool.release(conn)
    finally:
        await close_pool()

    failed = [r for r in results if not r[1]]
    print("=" * 46)
    print("AQUA-SENTINEL DATABASE VERIFICATION")
    print("=" * 46)
    print()
    for name, ok, detail in results:
        status = "OK" if ok else "FAIL"
        line = f"  {name:<48} {status}"
        if not ok:
            line += f"  ({detail})"
        print(line)
    print()
    if failed:
        print(f"{len(failed)} check(s) FAILED")
        return 1
    print("DATABASE VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))