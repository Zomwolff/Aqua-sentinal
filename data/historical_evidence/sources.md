# Historical Oil Spill Data — Source Inventory

Status as of research pass: 2026-08-27. Re-check licenses/URLs before ingesting at scale — things move.

## Tier 1 — Official / primary records

| Source | Coverage | Access | Notes |
|---|---|---|---|
| NOAA Office of Response & Restoration (ORR) | Primarily US, some international | Public via ERMA / IncidentNews web tools; bulk export historically limited | Widely cited as the largest structured global-ish incident record. Public machine-readable version reportedly only carries worst-case volume estimates, not confirmed actual release amounts. |
| Indian Coast Guard (ICG) | India | No consolidated public bulk dataset found | Individual incidents surface via press releases and news coverage referencing ICG statements. Treat each as `news_media`/tier-3 unless you obtain a primary ICG document. |
| National Green Tribunal (NGT) case records | India | Case-by-case, via NGT judgments | Useful for incidents that led to litigation (e.g. Ennore 2017/2023 cleanup orders); requires manual retrieval per case. |

## Tier 2 — Curated academic / industry datasets

| Source | Coverage | Access | Notes |
|---|---|---|---|
| Liu, Yin & Cai (2025), "Enhanced global oil spill dataset from 1967–2023," *Scientific Data* (DOI: 10.1038/s41597-025-05601-9) | Global, 3,550 incidents | Check paper's Data Availability section for the actual file location (typically Figshare/Zenodo for Sci Data papers) | Re-derives NOAA ORR data with corrected release-amount figures extracted from incident narrative text. Best single starting bulk dataset for non-India baseline/global context. |
| ITOPF | Global, tanker spills, since 1970 | Aggregate stats and named case histories public; full record-level database not public | Good for cross-checking volume/cause classification conventions, not a bulk source. |
| Cedre (France) | Global case-history "memory bank" | Case-by-case web access | Good corroborating source for well-known international spills. |
| EMSA CleanSeaNet | European waters, satellite detections | Restricted/institutional access | Not global, not relevant to India coverage, useful only as a methodology reference for satellite-detection confidence scoring later. |

## Tier 3 — News / secondary reporting (primary route for Indian coverage right now)

No consolidated open Indian government oil-spill database surfaced in this research pass. Practical path: **manually curate Indian incidents one at a time from primary news reporting**, cross-referencing at least two independent outlets where possible, and citing Wikipedia's spill-specific articles (which are themselves sourced and often carry precise coordinates) as a secondary check.

Confirmed real incidents identified so far (seeded in `seed_incidents.json`):

1. **2010 Mumbai Harbour spill** — MSC Chitra / Khalija III collision, ~7 Aug 2010, off Mumbai.
2. **2017 Ennore oil spill** — BW Maple / Dawn Kanchipuram collision, 28 Jan 2017, off Kamarajar Port, Chennai. Exact coordinates available (13.228166°N, 80.363333°E).
3. **2021 Palghar/Vadrai coast spill** — oil from a grounded accommodation barge (Cyclone Tauktae aftermath), late May 2021, Palghar district, Maharashtra.
4. **2023 Ennore Creek spill** — oily water/sludge following Cyclone Michaung, Dec 2023, Ennore Creek, Chennai.

Each should get its own `incident_sources` row per outlet consulted, so corroboration is auditable.

## Explicit rule on synthetic records

If Indian demo coverage is needed before enough real records exist:

- Set `is_synthetic = TRUE`, `confidence_level = 'synthetic_demo'`, and fill `synthetic_notes` explaining exactly what was fabricated and why (e.g. "location/date invented to demonstrate map clustering near Mumbai; not based on a real incident").
- Never backfill a synthetic record with a real foreign incident's details relabeled as Indian. If you want a "realistic" demo case, generate genuinely fictional coordinates/dates rather than reusing a real spill from elsewhere under a new label — that would misattribute a real event.
- The schema's CHECK constraint (`synthetic_must_be_flagged`) makes it structurally impossible to mark a record synthetic without an explanation, and impossible to use `synthetic_demo` confidence without the boolean flag.

## Deduplication approach for combining sources

When merging Tier 1/2/3 sources, match candidate duplicates on:
1. Date within ±2 days, AND
2. Location within ~25 km, AND
3. (if available) shared vessel name or IMO number.

Matches should be merged into one `incidents` row with multiple `incident_sources` entries (raising `confidence_level` toward `verified_multi_source`), not duplicated as separate rows.
