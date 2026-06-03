-- Malaria region tracker schema (SQLite, SCD Type-2).
-- Authoritative classification = CDC Travelers' Health feed; "endemic" for donor deferral
-- means CDC recommends antimalarial chemoprophylaxis there (FDA 12/2022).
-- Timestamps are ISO-8601 UTC ('...Z'); booleans are INTEGER 0/1; hashes are hex sha256.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Schema metadata
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Group A: source & fetch provenance (append-only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source (
  source_id      INTEGER PRIMARY KEY,
  code           TEXT NOT NULL UNIQUE,        -- 'cdc_yellowfever_json', 'fda_rules', 'geonames'
  name           TEXT NOT NULL,
  kind           TEXT NOT NULL,               -- 'http_json' | 'static_table' | 'manual'
  url            TEXT,
  authority_tier TEXT NOT NULL DEFAULT 'primary'  -- 'primary' | 'regulatory' | 'enrichment'
);

CREATE TABLE IF NOT EXISTS fetch_run (
  fetch_run_id      INTEGER PRIMARY KEY,
  source_id         INTEGER NOT NULL REFERENCES source(source_id),
  collection_date   TEXT NOT NULL,            -- logical day 'YYYY-MM-DD' (SCD2 valid_from anchor)
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  status            TEXT NOT NULL,            -- 'success'|'unchanged'|'http_error'|'parse_error'|'aborted'
  http_status       INTEGER,
  payload_sha256    TEXT,
  destination_count INTEGER,
  versions_written  INTEGER NOT NULL DEFAULT 0,
  changes_emitted   INTEGER NOT NULL DEFAULT 0,
  error_detail      TEXT,
  tool_version      TEXT NOT NULL,
  UNIQUE (source_id, collection_date)         -- one authoritative run per source per day
);
CREATE INDEX IF NOT EXISTS ix_fetch_run_source_date ON fetch_run(source_id, collection_date);

CREATE TABLE IF NOT EXISTS raw_payload (
  payload_sha256 TEXT PRIMARY KEY,            -- content address; identical days dedupe
  source_id      INTEGER NOT NULL REFERENCES source(source_id),
  first_seen_at  TEXT NOT NULL,
  byte_size      INTEGER NOT NULL,
  storage        TEXT NOT NULL DEFAULT 'file', -- 'file' | 'blob'
  file_path      TEXT,
  compression    TEXT NOT NULL DEFAULT 'zstd',
  body           BLOB
);

-- ---------------------------------------------------------------------------
-- Group B: country dimension (the join spine)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS country (
  country_id          INTEGER PRIMARY KEY,
  iso3                TEXT,                    -- ISO-3166 alpha-3, nullable until mapped
  iso2                TEXT,                    -- ISO-3166 alpha-2 (GeoNames join key)
  display_name        TEXT NOT NULL,
  is_subnational_only INTEGER NOT NULL DEFAULT 0,
  created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_country_iso3 ON country(iso3);
CREATE INDEX IF NOT EXISTS ix_country_iso2 ON country(iso2);

-- Maps each source's native key(s) to our country_id; isolates CDC id/slug drift.
CREATE TABLE IF NOT EXISTS country_alias (
  alias_id      INTEGER PRIMARY KEY,
  country_id    INTEGER NOT NULL REFERENCES country(country_id),
  source_id     INTEGER NOT NULL REFERENCES source(source_id),
  native_kind   TEXT NOT NULL,                -- 'cdc_destination_id'|'cdc_slug'|'cdc_name'|'iso3'
  native_key    TEXT NOT NULL,
  native_label  TEXT,
  first_seen_fetch_run_id INTEGER REFERENCES fetch_run(fetch_run_id),
  last_seen_fetch_run_id  INTEGER REFERENCES fetch_run(fetch_run_id),
  UNIQUE (source_id, native_kind, native_key)
);
CREATE INDEX IF NOT EXISTS ix_alias_country ON country_alias(country_id);

-- Reference crosswalk seeded from reference/country_iso3.csv (CDC slug -> ISO3).
CREATE TABLE IF NOT EXISTS cdc_iso3 (
  friendly_name TEXT PRIMARY KEY,             -- CDC FriendlyName (slug)
  cdc_name      TEXT,
  iso3          TEXT,                          -- territory's own ISO3 (nullable for unmapped entities)
  iso2          TEXT,                          -- territory's own ISO2 (GeoNames join key)
  note          TEXT
);

-- ---------------------------------------------------------------------------
-- Group C: versioned malaria record (SCD2 core)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS malaria_record (
  record_id      INTEGER PRIMARY KEY,
  country_id     INTEGER NOT NULL REFERENCES country(country_id),

  -- SCD2 validity
  valid_from     TEXT NOT NULL,
  valid_to       TEXT,
  is_current     INTEGER NOT NULL DEFAULT 1,

  -- provenance
  source_id      INTEGER NOT NULL REFERENCES source(source_id),
  fetch_run_id   INTEGER NOT NULL REFERENCES fetch_run(fetch_run_id),
  payload_sha256 TEXT NOT NULL REFERENCES raw_payload(payload_sha256),
  content_hash   TEXT NOT NULL,               -- sha256 over normalized derived model

  -- verbatim CDC fields (HTML preserved exactly as received)
  area_of_risk_html            TEXT,
  relative_risk_html           TEXT,
  chloroquine_resist_html      TEXT,
  species_html                 TEXT,
  recommended_prophylaxis_html TEXT,
  cdc_updated_date             TEXT,          -- parsed 'YYYY-MM-DD' (NOT part of content_hash)

  -- derived donor-screening classification
  has_transmission                INTEGER NOT NULL,
  is_endemic                      INTEGER NOT NULL,  -- = country_has_any_prophylaxis_area (the FDA bit)
  whole_country_endemic           INTEGER NOT NULL DEFAULT 0,
  country_has_any_prophylaxis_area INTEGER NOT NULL DEFAULT 0,
  screening_class                 TEXT NOT NULL,     -- 'whole_country'|'partial'|'none'
  prophylaxis_drugs_json          TEXT,              -- sorted JSON array
  species_json                    TEXT,              -- sorted JSON array
  chloroquine_resistant           INTEGER,           -- 1/0/NULL
  area_summary                    TEXT,              -- short human summary for lists

  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mr_asof ON malaria_record(country_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS ix_mr_hash ON malaria_record(country_id, content_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mr_one_current ON malaria_record(country_id) WHERE is_current = 1;

-- Parsed AreaOfRisk statements (children of a malaria_record version)
CREATE TABLE IF NOT EXISTS area_statement (
  area_statement_id INTEGER PRIMARY KEY,
  record_id     INTEGER NOT NULL REFERENCES malaria_record(record_id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  raw_text      TEXT NOT NULL,                -- verbatim li text (entities decoded, tags stripped)
  polarity      TEXT NOT NULL,               -- 'include' | 'exclude'
  tier          TEXT NOT NULL,               -- 'prophylaxis' | 'sporadic' | 'none'
  scope         TEXT,                         -- 'all' | 'region' | 'city' | 'district' | 'other'
  place_name    TEXT,
  elev_max_m    INTEGER,
  elev_min_m    INTEGER,
  season_text   TEXT,
  UNIQUE (record_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_area_record ON area_statement(record_id);

-- ---------------------------------------------------------------------------
-- Group E: yellow fever (captured for free; same SCD2 shape; secondary)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS yellowfever_record (
  record_id      INTEGER PRIMARY KEY,
  country_id     INTEGER NOT NULL REFERENCES country(country_id),
  valid_from     TEXT NOT NULL,
  valid_to       TEXT,
  is_current     INTEGER NOT NULL DEFAULT 1,
  source_id      INTEGER NOT NULL REFERENCES source(source_id),
  fetch_run_id   INTEGER NOT NULL REFERENCES fetch_run(fetch_run_id),
  payload_sha256 TEXT NOT NULL REFERENCES raw_payload(payload_sha256),
  content_hash   TEXT NOT NULL,
  has_requirements    INTEGER,
  requirements_html   TEXT,
  has_recommendations INTEGER,
  recommendations_html TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_yf_asof ON yellowfever_record(country_id, valid_from, valid_to);
CREATE UNIQUE INDEX IF NOT EXISTS uq_yf_one_current ON yellowfever_record(country_id) WHERE is_current = 1;

-- ---------------------------------------------------------------------------
-- Group F: change feed
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS change_event (
  change_event_id     INTEGER PRIMARY KEY,
  collection_date     TEXT NOT NULL,
  fetch_run_id        INTEGER NOT NULL REFERENCES fetch_run(fetch_run_id),
  entity              TEXT NOT NULL,          -- 'malaria' | 'yellowfever' | 'country'
  country_id          INTEGER REFERENCES country(country_id),
  country_name        TEXT,                   -- denormalized for a self-contained feed
  change_type         TEXT NOT NULL,          -- country_added|country_removed|became_endemic
                                              -- |became_non_endemic|field_changed
  changed_fields_json TEXT,
  old_value           TEXT,
  new_value           TEXT,
  created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_change_date    ON change_event(collection_date);
CREATE INDEX IF NOT EXISTS ix_change_country ON change_event(country_id);

-- ---------------------------------------------------------------------------
-- Group G: FDA deferral rules (regulatory reference, versioned, seeded from yaml)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deferral_rule (
  rule_id        INTEGER PRIMARY KEY,
  source_id      INTEGER REFERENCES source(source_id),
  code           TEXT NOT NULL,
  exposure_type  TEXT,                         -- 'residence' | 'travel' | 'history'
  geo_scope      TEXT,                         -- 'whole_country' | 'endemic_area_only'
  threshold      TEXT,
  deferral_window TEXT,
  description    TEXT NOT NULL,
  citation       TEXT,
  valid_from     TEXT,
  valid_to       TEXT
);

-- ---------------------------------------------------------------------------
-- Full-text search over current malaria records (rebuilt each collection run)
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS malaria_fts USING fts5(
  display_name, iso3, area_text, species, drugs, tokenize='porter'
);

-- ---------------------------------------------------------------------------
-- Views (Datasette serves these as read-only tables / JSON endpoints)
-- ---------------------------------------------------------------------------

-- Current malaria state, one row per country.
CREATE VIEW IF NOT EXISTS v_malaria_current AS
SELECT
  c.country_id, c.iso3, c.iso2, c.display_name,
  mr.has_transmission, mr.is_endemic, mr.whole_country_endemic,
  mr.country_has_any_prophylaxis_area, mr.screening_class,
  mr.area_summary, mr.species_json, mr.prophylaxis_drugs_json, mr.chloroquine_resistant,
  mr.area_of_risk_html, mr.recommended_prophylaxis_html, mr.species_html, mr.chloroquine_resist_html,
  mr.cdc_updated_date, mr.valid_from AS first_seen_on, mr.record_id
FROM malaria_record mr
JOIN country c USING (country_id)
WHERE mr.is_current = 1;

-- Compact current-state feed for the choropleth map (one row per country with ISO3).
CREATE VIEW IF NOT EXISTS country_current AS
SELECT c.iso3, c.iso2, c.display_name,
       mr.is_endemic, mr.screening_class, mr.area_summary, mr.cdc_updated_date
FROM malaria_record mr
JOIN country c USING (country_id)
WHERE mr.is_current = 1;

-- Full version history (current + superseded), newest first per country.
CREATE VIEW IF NOT EXISTS v_malaria_history AS
SELECT c.display_name, c.iso3, mr.record_id, mr.valid_from, mr.valid_to, mr.is_current,
       mr.screening_class, mr.is_endemic, mr.area_summary, mr.cdc_updated_date,
       mr.fetch_run_id, mr.content_hash
FROM malaria_record mr
JOIN country c USING (country_id)
ORDER BY c.display_name, mr.valid_from DESC;
