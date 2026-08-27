# Historical Oil Spill Incident Layer — Starter Kit

Files:

- **`schema.sql`** — PostGIS schema for `sources`, `incidents`, `incident_sources`, plus a `v_incident_summary` view and a ready-to-adapt "nearby incidents" query. Enforces (via CHECK constraint) that a record cannot be marked `synthetic_demo` without also setting `is_synthetic = TRUE` and filling in `synthetic_notes` — so a fabricated demo record can't silently pass as real.
- **`sources.md`** — inventory of candidate data sources (NOAA/ORR, the Purdue/*Scientific Data* enhanced global dataset, ITOPF, Cedre, and the reality that India has no consolidated open government spill database yet), plus the dedup rule for combining sources and the rule for handling synthetic records.
- **`seed_incidents.json`** — 4 confirmed real Indian incidents (2010 Mumbai, 2017 Ennore, 2021 Palghar/Tauktae, 2023 Ennore/Michaung), each with a confidence level reflecting how corroborated it currently is, and no synthetic records mixed in.
- **`load_seed.py`** — loads the JSON into a running PostGIS database matching `schema.sql`.

## Suggested next steps

1. Stand up Postgres+PostGIS, run `schema.sql`.
2. `pip install psycopg2-binary --break-system-packages` then `python load_seed.py --dsn "postgresql://..."`.
3. Add more real incidents (India first, since that's your gap) — one `incidents` row + one or more `incident_sources` rows per record, promoting `confidence_level` to `verified_multi_source` once 2+ independent sources agree.
4. Build the two backend endpoints your frontend actually needs first:
   - `GET /incidents/near?lat=&lon=&radius_km=` → uses the `ST_DWithin` query at the bottom of `schema.sql`.
   - `GET /incidents/:id` → full record for a detail view.
5. Only once you have real coverage in a region should you add synthetic demo records there, and only ever with `is_synthetic = TRUE` — never by relabeling a real foreign incident as Indian.
6. Historical-occurrence context should stay presentational for now — don't wire it into the live spill-candidate score until you're deliberately building the evidence-comparison layer (stage 2/3 in your plan).
