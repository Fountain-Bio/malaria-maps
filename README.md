# Malaria Region Tracker

A backend that tracks malaria endemic-region data for US blood-donor geographic deferral
screening, built entirely from **public-domain CDC primary data**. It collects daily,
versions every change, and serves a read-only API plus an explorable UI with a world map.

## Why this exists

Donor screening defers donors based on malaria-endemic residence and travel. The FDA
defines "malaria-endemic" (Guidance for Industry, *Recommendations to Reduce the Risk of
Transfusion-Transmitted Malaria*, final 12/2022) as anywhere **CDC recommends antimalarial
chemoprophylaxis**. So CDC's per-country recommendation is the operative definition, and
this tracker reads it directly from the source CDC publishes:

```
https://wwwnc.cdc.gov/travel/Services/xmlservices.asmx/YellowFeverInformationJson
```

A correctness point baked into the classifier: `HasTransmission = true` is **not** the same
as "deferrable." Of 90 countries with transmission, only 83 have an area where CDC
recommends chemoprophylaxis; 7 (e.g. Greece, Syria, Suriname) have transmission but
"no chemoprophylaxis recommended," so they are not endemic for deferral. The parser keys
the deferral classification off `RecommendedProphylaxis`, not the transmission boolean, and
always preserves the verbatim CDC text for the human screener.

WHO and the Malaria Atlas Project measure epidemiological endemicity, which is a different
question, so they are not used to drive classification.

## Architecture

```
CDC JSON endpoint ──> collect ──> versioned SQLite (data/malaria.db) ──> Datasette ──> API + UI + map
   (daily)         parse + SCD2       history + change feed             read-only
                   + raw archive
```

- **Versioning:** slowly-changing-dimension type-2. A country gets a new `malaria_record`
  version only when its normalized content changes (a date-only republish does not create a
  version). This answers "what was the endemic list on date X" and "what changed between two
  dates" cheaply.
- **Provenance:** every derived row links to the archived raw payload (`data/raw/`,
  content-addressed, zstd) and the `fetch_run` that produced it.
- **Change feed:** `change_event` records `country_added`, `became_endemic`,
  `field_changed`, etc.

## Quickstart

```bash
uv sync                                       # install deps
echo 'GEONAMES_USERNAME=your_geonames_user' > .env   # free GeoNames account, for city lookup
uv run python -m malaria_tracker.collect      # fetch CDC -> versioned SQLite (idempotent)
uv run datasette data/malaria.db -m metadata.yaml --static web:web/ --plugins-dir plugins --port 8765
```

Then open:

- **Map:** http://127.0.0.1:8765/web/index.html — red = whole-country endemic, amber =
  endemic in specific areas, grey = not endemic. Click a country for detail + history, or use
  the **search box** to check whether a specific city is in a deferral geography.
- **Datasette home:** http://127.0.0.1:8765/malaria

City lookup also works from the terminal: `uv run python -m malaria_tracker.locate "Nairobi"`.

Run the collector daily (manual, or a local cron entry). It is idempotent: a second run the
same day with an unchanged payload writes nothing.

## API (read-only, via Datasette)

Every table, view, and canned query is available as JSON by appending `.json`
(`?_shape=array` for a plain list).

| Need | Endpoint |
|---|---|
| Current status, one row/country | `/malaria/v_malaria_current.json?_shape=array` |
| Map feed (ISO3 + class) | `/malaria/country_current.json?_shape=array&_size=max` |
| One country | `/malaria/v_malaria_current.json?_shape=array&iso3=AFG` |
| Changes since a date | `/malaria/changes_since.json?since=2026-01-01&_shape=array` |
| Endemic list as of a date | `/malaria/endemic_list_as_of.json?as_of=2026-01-01&_shape=array` |
| Country version history | `/malaria/country_history.json?country=Afghanistan&_shape=array` |
| Full-text area search | `/malaria/search_areas.json?q=gold+mining&_shape=array` |
| Collection provenance | `/malaria/fetch_run.json?_shape=array` |
| City deferral lookup | `/-/locate?q=Nairobi` (or `?geonameId=<id>`) |

Datasette also gives faceted browse, FTS, and a read-only SQL box for ad-hoc queries.

### City lookup (`/-/locate`)

A Datasette plugin (`plugins/locate_endpoint.py`) geocodes a place via GeoNames (server-side,
username from `.env`), maps it to its country, and returns a residence verdict and a travel
verdict with the CDC basis. Residence is country-level (any city in a country with an endemic
area defers a >5-year resident); travel is decided against the parsed sub-national areas. When
a city in an endemic country can't be placed confidently, travel is reported as `uncertain`
(never a false "not deferred"), and the verbatim CDC text is always returned. Geocoding by
GeoNames (CC BY).

## Data model

- `malaria_record` — SCD2 versioned classification (verbatim CDC HTML + derived fields:
  `is_endemic`, `whole_country_endemic`, `screening_class`, `prophylaxis_drugs_json`,
  `species_json`, `chloroquine_resistant`).
- `area_statement` — parsed sub-national risk statements (`polarity`, `tier` =
  prophylaxis/sporadic/none, elevation, season, excluded cities).
- `yellowfever_record` — captured from the same feed (secondary).
- `country` / `country_alias` / `cdc_iso3` — country spine + CDC→ISO3 crosswalk.
- `change_event` — the change feed. `fetch_run` / `raw_payload` — provenance.
- `deferral_rule` — FDA 12/2022 rules as versioned reference (so the pending Jan-2025
  testing-based draft can be added without redesign).
- Views: `v_malaria_current`, `country_current`, `v_malaria_history`. FTS: `malaria_fts`.

## Reproducing the crosswalk

`reference/country_iso3.csv` maps CDC slugs to ISO-3166 alpha-3 (auto-matched against the
Natural Earth polygons, with a verified override list for small states and dependencies):

```bash
uv run python tools/build_crosswalk.py
```

## Tests and linting

```bash
uv run pytest        # unit tests
uv run ruff check    # static analysis (E/F/I/UP/B/SIM/C4)
```

Covers parser tiering (Afghanistan/Botswana/Syria/Greece), content-hash stability,
collector idempotency, SCD2 version open/close, the change feed, removals, and as-of queries.

## Deploy (Railway)

The serving side is packaged as a baked, read-only image. `data/malaria.db` is copied into
the image and opened immutable (`-i`), so each deploy is a fixed snapshot that a CDN can cache
hard. `Dockerfile` installs deps with uv and runs Datasette against that immutable DB with the
`web/` static mount and the local plugins dir. `railway.toml` selects the Dockerfile builder
and health-checks `/-/versions.json` (a plain 200; `/malaria` 302-redirects under hashed URLs,
so it isn't a stable check). It runs a single replica.

Caching is designed so no cache purge is ever required, which matters because intermediate
caches between the CDN and the browser can't be purged:

- The API is served under `/malaria-<hash>/…` by `datasette-hashed-urls` with a one-year
  `Cache-Control: max-age=31536000, public`. The hash is of the DB contents, so a rebake
  changes every URL and stale entries simply stop being requested. Unhashed `/malaria/…`
  paths 302 to the current hash.
- Static assets get headers from `plugins/cache_headers.py`: `world.geojson` and `app.js`
  go out immutable for a year, while `index.html` is `no-cache` so it always revalidates.

Operational notes:

- **Set `GEONAMES_USERNAME`** as a Railway service variable for the `/-/locate` city lookup.
  Without it, `/-/locate` returns 502 and the rest of the app is unaffected.
- **Weekly refresh:** `.github/workflows/weekly-collect.yml` runs the collector on a Monday
  cron, checkpoints the WAL, and commits `data/malaria.db` only when it changed. That commit
  is the deploy trigger once the repo is connected to Railway.
- **Geocode cache:** `data/geocode_cache.sqlite` is written at runtime and is ephemeral on
  Railway, rebuilt from GeoNames on demand. Mount a small volume at `/app/data` if you want
  it to persist across deploys and save GeoNames credits.

Build and check it locally:

```bash
docker build -t malaria-tracker .
docker run --rm -p 8899:8765 -e GEONAMES_USERNAME=your_user malaria-tracker
# map: http://127.0.0.1:8899/web/index.html
```

## Scope and limitations

- **Collection still runs as a CLI.** The collector is host-agnostic and writes the versioned
  SQLite file, run locally or by the weekly GitHub workflow. The reader is Datasette over the
  baked DB. See [Deploy (Railway)](#deploy-railway) for packaging.
- **Map polygons.** The choropleth uses Natural Earth 110m country polygons. French/UK/NL
  overseas territories with their own CDC entry (e.g. French Guiana) have ISO3 in the data
  but no separate 110m polygon, so they appear in the table/API but not as a distinct map
  fill. Country-level coloring is complete.
- **Area parsing is best-effort.** Verbatim CDC text is always stored and shown; the parsed
  structure is an aid, not a replacement for the source text.
