# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A backend that tracks malaria endemic-region data for US blood-donor geographic deferral screening. It collects daily from CDC, versions every change into SQLite, and serves a read-only API plus an explorable UI with a world choropleth.

## Commands

```bash
uv sync                                            # install deps (Python 3.12+, uv)

# Collect: fetch CDC -> parse -> versioned SQLite. Idempotent (re-run same day = no-op).
uv run python -m malaria_tracker.collect           # live fetch into data/malaria.db
uv run python -m malaria_tracker.collect --fixture tests/fixtures/cdc_yellowfever_raw.txt --date 2026-06-03
#   --fixture <file>  read a local payload instead of hitting the network
#   --date YYYY-MM-DD override collection_date (the SCD2 valid_from anchor)
#   --db <path>       target a different database file

# Serve: read-only API + faceted UI + the map (Datasette opens the file read-only)
uv run datasette data/malaria.db -m metadata.yaml --static web:web/ --port 8765
#   map: http://127.0.0.1:8765/web/index.html   home: http://127.0.0.1:8765/malaria

uv run pytest                                      # all tests
uv run pytest tests/test_parse.py::test_botswana_partial_with_tiers   # a single test
uv run python tools/build_crosswalk.py             # regenerate reference/country_iso3.csv
```

The collector is the only writer; restart Datasette after a collection run so it re-reads. There is no hosting/scheduling yet — the collector is a host-agnostic CLI meant to be run daily.

## The one rule that must not be broken

The deferral definition is **"CDC recommends antimalarial chemoprophylaxis"** (FDA 12/2022), which is **not** the same as `Malaria.HasTransmission`. Of 90 transmission countries, only 83 are deferrable; 7 have transmission but no recommended chemoprophylaxis and must stay out of the endemic set. The classifier therefore derives `is_endemic` / `country_has_any_prophylaxis_area` from the **`RecommendedProphylaxis`** field (an `<li>` either lists drug names or says "no chemoprophylaxis recommended"), never from the transmission boolean. WHO and the Malaria Atlas Project measure epidemiological endemicity, a different question, so they are not authoritative for classification. Verbatim CDC text is always stored alongside parsed structure; never replace it with parsed-only data.

## Data flow and architecture

```
CDC JSON endpoint ──> collect.py ──> data/malaria.db (SCD2) ──> Datasette ──> API + UI + web/ map
   (daily)        parse.py + db.py     history + change feed
```

- **Source** (`sources/cdc_travel.py`): the CDC endpoint returns a SOAP-style XML envelope (`<string>…</string>`) whose text is the JSON array, with HTML field values JSON-escaped. `parse_payload` unwraps the envelope, `json.loads`, and validates into `models.CdcDestination`, with a direct-JSON fallback.
- **Parse** (`parse.py`): turns the per-field HTML fragments (selectolax) into the normalized `MalariaDerived` model. `AreaOfRisk` `<li>`s become `area_statement` rows tiered as `prophylaxis` / `sporadic` / `none` with elevation/season/excluded-city annotations. `screening_class` ∈ {`whole_country`, `partial`, `none`} drives the map's red/amber/grey.
- **Versioning** (`db.py` + `schema.sql`): slowly-changing-dimension type-2 at the per-country-record grain, keyed by `content_hash`. A new `malaria_record` version is written only when the normalized derived content changes. **`content_hash` deliberately excludes `cdc_updated_date`** (`models.MalariaDerived.canonical_for_hash`), so a date-only republish creates no version. As-of queries use `valid_from <= X AND (valid_to > X OR valid_to IS NULL)`; a partial unique index enforces one current version per country.
- **Collection invariants** (`collect.py::run_collection`): everything runs in one transaction; abort guards (`status != 200`, unparseable, or `destination_count < DEST_FLOOR`≈200) stop before any mutation so a bad CDC day never mass-emits false removals; the raw payload is archived content-addressed (zstd) under `data/raw/`; changes are diffed into `change_event`; a country present before but absent this run is closed and recorded as `country_removed`. `run_collection` takes `dest_floor` so tests can lower it.
- **Country identity** (`db.resolve_or_create_country`): CDC `DestinationId`/slug/name can drift, so countries are keyed by a surrogate `country_id` resolved through `country_alias` (id → slug → normalized name). `country.iso3` is backfilled from `reference/country_iso3.csv` (the `cdc_iso3` table) at seed time; that crosswalk is on the critical path because the map joins polygons by ISO3.

## Serving and the map

Datasette is configured by `metadata.yaml`: facets, FTS5 (`malaria_fts`, rebuilt each run from current records + verbatim area text), HTML rendering for the `*_html` columns, and canned queries (`changes_since`, `endemic_list_as_of`, `country_history`, `search_areas`). The map (`web/index.html` + `web/app.js`, MapLibre, no API key) fetches `/malaria/country_current.json` and the Natural Earth polygons in `web/world.geojson`, joins on ISO3, and opens a per-country detail panel fetched from `v_malaria_current`. Overseas territories with their own CDC entry (e.g. French Guiana) have ISO3 in the data but no separate 110m polygon, so they appear in the table/API but not as a distinct map fill.

## Conventions

- Plain stdlib `sqlite3` (no ORM); the SCD2 close/open SQL is intentionally explicit in `db.py`. Schema lives in `schema.sql` and is applied via `db.init_schema`.
- Pydantic models in `models.py` separate the raw CDC payload from the derived classification; the derived model owns the canonical-JSON hashing contract.
- `reference/fda_rules.json` seeds the `deferral_rule` table as versioned regulatory reference; rules carry `valid_from`/`valid_to` so a future testing-based FDA model can be added without redesign.
- Lean dependencies only (`httpx`, `selectolax`, `pydantic`, `zstandard`, `datasette` + render plugin). `data/raw/` is gitignored; `data/malaria.db` is committed as the versioned deliverable (checkpoint the WAL before committing it).
