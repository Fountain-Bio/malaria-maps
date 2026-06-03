"""SQLite repository: pragmas, schema init, SCD2 close/open, country resolution, FTS.

Plain stdlib sqlite3 (no ORM): the schema is small and hand-tuned (partial unique
indexes, explicit transactions for SCD2 atomicity), and the close/open SQL is the heart
of the layer, so it stays visible.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any

SCHEMA_RESOURCE = "schema.sql"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit off via explicit BEGIN
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("malaria_tracker").joinpath(SCHEMA_RESOURCE).read_text(encoding="utf-8")
    conn.executescript(sql)
    cur = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'")
    if cur.fetchone() is None:
        conn.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')")


# --------------------------------------------------------------------------- sources
def upsert_source(conn: sqlite3.Connection, code: str, name: str, kind: str,
                  url: str | None, authority_tier: str) -> int:
    conn.execute(
        "INSERT INTO source(code, name, kind, url, authority_tier) VALUES (?,?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, kind=excluded.kind, "
        "url=excluded.url, authority_tier=excluded.authority_tier",
        (code, name, kind, url, authority_tier),
    )
    return source_id(conn, code)


def source_id(conn: sqlite3.Connection, code: str) -> int:
    row = conn.execute("SELECT source_id FROM source WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise KeyError(f"unknown source code: {code}")
    return int(row["source_id"])


# --------------------------------------------------------------------------- crosswalk
def load_cdc_iso3(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    n = 0
    for r in rows:
        conn.execute(
            "INSERT INTO cdc_iso3(friendly_name, cdc_name, iso3, note) VALUES (?,?,?,?) "
            "ON CONFLICT(friendly_name) DO UPDATE SET cdc_name=excluded.cdc_name, "
            "iso3=excluded.iso3, note=excluded.note",
            (r["friendly_name"], r.get("cdc_name"), r.get("iso3") or None, r.get("note")),
        )
        n += 1
    return n


def iso3_for_slug(conn: sqlite3.Connection, slug: str) -> str | None:
    row = conn.execute("SELECT iso3 FROM cdc_iso3 WHERE friendly_name = ?", (slug,)).fetchone()
    return row["iso3"] if row and row["iso3"] else None


# --------------------------------------------------------------------------- country resolution
def resolve_or_create_country(
    conn: sqlite3.Connection, source_id_: int, fetch_run_id: int,
    destination_id: int, slug: str, name: str, now: str,
) -> tuple[int, bool]:
    """Resolve a CDC destination to a country_id via alias chain (id -> slug -> name).

    Returns (country_id, created).
    """
    for kind, key in (("cdc_destination_id", str(destination_id)), ("cdc_slug", slug),
                      ("cdc_name", _norm_name(name))):
        row = conn.execute(
            "SELECT country_id FROM country_alias WHERE source_id=? AND native_kind=? AND native_key=?",
            (source_id_, kind, key),
        ).fetchone()
        if row:
            cid = int(row["country_id"])
            _touch_aliases(conn, source_id_, cid, fetch_run_id)
            return cid, False

    iso3 = iso3_for_slug(conn, slug)
    cur = conn.execute(
        "INSERT INTO country(iso3, display_name, created_at) VALUES (?,?,?)",
        (iso3, name, now),
    )
    cid = int(cur.lastrowid)
    for kind, key in (("cdc_destination_id", str(destination_id)), ("cdc_slug", slug),
                      ("cdc_name", _norm_name(name))):
        conn.execute(
            "INSERT INTO country_alias(country_id, source_id, native_kind, native_key, "
            "native_label, first_seen_fetch_run_id, last_seen_fetch_run_id) VALUES (?,?,?,?,?,?,?)",
            (cid, source_id_, kind, key, name, fetch_run_id, fetch_run_id),
        )
    return cid, True


def _touch_aliases(conn: sqlite3.Connection, source_id_: int, country_id: int, fetch_run_id: int) -> None:
    conn.execute(
        "UPDATE country_alias SET last_seen_fetch_run_id=? WHERE source_id=? AND country_id=?",
        (fetch_run_id, source_id_, country_id),
    )


def _norm_name(name: str) -> str:
    return " ".join(name.lower().split())


def country_ids_seen_since(conn: sqlite3.Connection, source_id_: int, fetch_run_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT DISTINCT country_id FROM country_alias "
        "WHERE source_id=? AND last_seen_fetch_run_id=?",
        (source_id_, fetch_run_id),
    ).fetchall()
    return {int(r["country_id"]) for r in rows}


# --------------------------------------------------------------------------- SCD2 core
def current_version(conn: sqlite3.Connection, table: str, country_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {table} WHERE country_id=? AND is_current=1", (country_id,)
    ).fetchone()


def close_version(conn: sqlite3.Connection, table: str, record_id: int, valid_to: str) -> None:
    conn.execute(
        f"UPDATE {table} SET is_current=0, valid_to=? WHERE record_id=?", (valid_to, record_id)
    )


def insert_version(conn: sqlite3.Connection, table: str, fields: dict[str, Any]) -> int:
    cols = ", ".join(fields.keys())
    qs = ", ".join("?" for _ in fields)
    cur = conn.execute(f"INSERT INTO {table}({cols}) VALUES ({qs})", tuple(fields.values()))
    return int(cur.lastrowid)


def all_current_country_ids(conn: sqlite3.Connection, table: str) -> set[int]:
    rows = conn.execute(f"SELECT country_id FROM {table} WHERE is_current=1").fetchall()
    return {int(r["country_id"]) for r in rows}


# --------------------------------------------------------------------------- change feed
def record_change(conn: sqlite3.Connection, *, collection_date: str, fetch_run_id: int,
                  entity: str, country_id: int | None, country_name: str | None,
                  change_type: str, changed_fields: list[str] | None,
                  old_value: str | None, new_value: str | None, now: str) -> None:
    import json
    conn.execute(
        "INSERT INTO change_event(collection_date, fetch_run_id, entity, country_id, country_name, "
        "change_type, changed_fields_json, old_value, new_value, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (collection_date, fetch_run_id, entity, country_id, country_name, change_type,
         json.dumps(changed_fields) if changed_fields else None, old_value, new_value, now),
    )


# --------------------------------------------------------------------------- raw payload
def archive_exists(conn: sqlite3.Connection, payload_sha256: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM raw_payload WHERE payload_sha256=?", (payload_sha256,)
    ).fetchone() is not None


def insert_raw_payload(conn: sqlite3.Connection, *, payload_sha256: str, source_id_: int,
                       first_seen_at: str, byte_size: int, file_path: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO raw_payload(payload_sha256, source_id, first_seen_at, byte_size, "
        "storage, file_path, compression) VALUES (?,?,?,?, 'file', ?, 'zstd')",
        (payload_sha256, source_id_, first_seen_at, byte_size, file_path),
    )


# --------------------------------------------------------------------------- fetch_run
def find_run(conn: sqlite3.Connection, source_id_: int, collection_date: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM fetch_run WHERE source_id=? AND collection_date=?",
        (source_id_, collection_date),
    ).fetchone()


def start_run(conn: sqlite3.Connection, source_id_: int, collection_date: str,
              started_at: str, tool_version: str) -> int:
    cur = conn.execute(
        "INSERT INTO fetch_run(source_id, collection_date, started_at, status, tool_version) "
        "VALUES (?,?,?, 'running', ?) "
        "ON CONFLICT(source_id, collection_date) DO UPDATE SET started_at=excluded.started_at, "
        "status='running'",
        (source_id_, collection_date, started_at, tool_version),
    )
    if cur.lastrowid:
        return int(cur.lastrowid)
    row = find_run(conn, source_id_, collection_date)
    assert row is not None
    return int(row["fetch_run_id"])


def finish_run(conn: sqlite3.Connection, fetch_run_id: int, **fields: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE fetch_run SET {sets} WHERE fetch_run_id=?",
                 (*fields.values(), fetch_run_id))


# --------------------------------------------------------------------------- FTS
def rebuild_malaria_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM malaria_fts")
    conn.execute(
        "INSERT INTO malaria_fts(rowid, display_name, iso3, area_text, species, drugs) "
        "SELECT mr.record_id, c.display_name, COALESCE(c.iso3,''), "
        "       TRIM(COALESCE(mr.area_summary,'') || ' ' || "
        "            COALESCE((SELECT group_concat(a.raw_text, ' ') FROM area_statement a "
        "                      WHERE a.record_id = mr.record_id), '')), "
        "       COALESCE(mr.species_json,''), COALESCE(mr.prophylaxis_drugs_json,'') "
        "FROM malaria_record mr JOIN country c USING(country_id) WHERE mr.is_current=1"
    )
