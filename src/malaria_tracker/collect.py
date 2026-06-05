"""Daily collection: fetch CDC -> guard -> archive -> SCD2 upsert -> change feed.

Idempotent: a second run the same day with an unchanged payload writes zero versions and
zero change events. All mutation happens in one transaction per run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import zstandard as zstd

from . import __version__, db
from .models import CdcDestination
from .parse import derive_malaria, derive_yellowfever
from .sources import cdc_travel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REF_DIR = PROJECT_ROOT / "reference"
DEST_FLOOR = 200  # abort if the feed returns fewer than this (expected ~244)

SOURCES = [
    ("cdc_yellowfever_json", "CDC Travelers' Health Yellow Fever & Malaria feed",
     "http_json", cdc_travel.DEFAULT_URL, "primary"),
    ("fda_rules", "FDA Transfusion-Transmitted Malaria guidance (12/2022)",
     "manual", "https://www.fda.gov/media/163737/download", "regulatory"),
    ("geonames", "GeoNames country / ISO3 reference", "static_table",
     "https://download.geonames.org/export/dump/", "enrichment"),
]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- seeding
def init_and_seed(conn: db.sqlite3.Connection) -> None:
    db.init_schema(conn)
    conn.execute("BEGIN")
    try:
        for code, name, kind, url, tier in SOURCES:
            db.upsert_source(conn, code, name, kind, url, tier)
        _seed_rules(conn)
        _seed_crosswalk(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _seed_rules(conn: db.sqlite3.Connection) -> None:
    path = REF_DIR / "fda_rules.json"
    if not path.exists():
        return
    rules = json.loads(path.read_text(encoding="utf-8"))
    sid = db.source_id(conn, "fda_rules")
    conn.execute("DELETE FROM deferral_rule WHERE source_id=?", (sid,))
    for r in rules:
        conn.execute(
            "INSERT INTO deferral_rule(source_id, code, exposure_type, geo_scope, threshold, "
            "deferral_window, pathogen_reduction, description, citation, valid_from, valid_to) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, r["code"], r.get("exposure_type"), r.get("geo_scope"), r.get("threshold"),
             r.get("deferral_window"), r.get("pathogen_reduction"), r["description"],
             r.get("citation"), r.get("valid_from"), r.get("valid_to")),
        )


def _seed_crosswalk(conn: db.sqlite3.Connection) -> None:
    import csv
    path = REF_DIR / "country_iso3.csv"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    db.load_cdc_iso3(conn, rows)
    # Backfill ISO3 / ISO2 onto existing country rows.
    for r in rows:
        if r.get("iso3"):
            conn.execute(
                "UPDATE country SET iso3=COALESCE(iso3, ?), iso2=COALESCE(iso2, ?) "
                "WHERE country_id IN (SELECT country_id FROM country_alias "
                "WHERE native_kind='cdc_slug' AND native_key=?)",
                (r["iso3"], r.get("iso2"), r["friendly_name"]),
            )


# --------------------------------------------------------------------------- collection
def _malaria_fields(cid: int, d: CdcDestination, derived, *, valid_from, sid, run_id,
                    payload_sha, content_hash, now) -> dict:
    return {
        "country_id": cid, "valid_from": valid_from, "valid_to": None, "is_current": 1,
        "source_id": sid, "fetch_run_id": run_id, "payload_sha256": payload_sha,
        "content_hash": content_hash,
        "area_of_risk_html": d.malaria.area_of_risk,
        "relative_risk_html": d.malaria.relative_risk,
        "chloroquine_resist_html": d.malaria.chloroquine_resistance,
        "species_html": d.malaria.species,
        "recommended_prophylaxis_html": d.malaria.recommended_prophylaxis,
        "cdc_updated_date": derived.cdc_updated_date,
        "has_transmission": int(derived.has_transmission),
        "is_endemic": int(derived.is_endemic),
        "whole_country_endemic": int(derived.whole_country_endemic),
        "country_has_any_prophylaxis_area": int(derived.country_has_any_prophylaxis_area),
        "screening_class": derived.screening_class,
        "prophylaxis_drugs_json": json.dumps(sorted(derived.prophylaxis_drugs)),
        "species_json": json.dumps(sorted(derived.species)),
        "chloroquine_resistant": None if derived.chloroquine_resistant is None
                                  else int(derived.chloroquine_resistant),
        "area_summary": derived.area_summary,
        "created_at": now,
    }


def _insert_area_statements(conn, record_id: int, derived) -> None:
    for a in derived.area_statements:
        conn.execute(
            "INSERT INTO area_statement(record_id, seq, raw_text, polarity, tier, scope, "
            "place_name, elev_max_m, elev_min_m, season_text) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record_id, a.seq, a.raw_text, a.polarity, a.tier, a.scope, a.place_name,
             a.elev_max_m, a.elev_min_m, a.season_text),
        )


def _diff_malaria(prev, derived) -> list[str]:
    changed = []
    if bool(prev["is_endemic"]) != derived.is_endemic:
        changed.append("is_endemic")
    if prev["screening_class"] != derived.screening_class:
        changed.append("screening_class")
    if (prev["species_json"] or "[]") != json.dumps(sorted(derived.species)):
        changed.append("species")
    if (prev["prophylaxis_drugs_json"] or "[]") != json.dumps(sorted(derived.prophylaxis_drugs)):
        changed.append("prophylaxis_drugs")
    pr = prev["chloroquine_resistant"]
    if (None if pr is None else bool(pr)) != derived.chloroquine_resistant:
        changed.append("chloroquine_resistant")
    if (prev["area_summary"] or "") != (derived.area_summary or ""):
        changed.append("area_summary")
    return changed


def run_collection(conn, fetch_result: cdc_travel.FetchResult, *, collection_date: str,
                   now: str, raw_dir: Path, dest_floor: int = DEST_FLOOR) -> dict:
    sid = db.source_id(conn, "cdc_yellowfever_json")
    fr = fetch_result

    existing = db.find_run(conn, sid, collection_date)
    if existing and existing["status"] in ("success", "unchanged") \
            and existing["payload_sha256"] == fr.sha256:
        return {"status": "noop", "reason": "already collected today with identical payload"}

    run_id = db.start_run(conn, sid, collection_date, now, __version__)

    if fr.http_status != 200:
        db.finish_run(conn, run_id, status="http_error", http_status=fr.http_status,
                      finished_at=_now())
        return {"status": "http_error", "http_status": fr.http_status}

    dests = fr.destinations
    if len(dests) < dest_floor:
        db.finish_run(conn, run_id, status="aborted", http_status=200,
                      destination_count=len(dests), finished_at=_now(),
                      error_detail=f"destination_count {len(dests)} < floor {dest_floor}")
        return {"status": "aborted", "destination_count": len(dests)}

    conn.execute("BEGIN")
    try:
        if not db.archive_exists(conn, fr.sha256):
            raw_dir.mkdir(parents=True, exist_ok=True)
            fpath = raw_dir / f"{collection_date}__{fr.sha256[:12]}.json.zst"
            fpath.write_bytes(zstd.ZstdCompressor(level=10).compress(fr.raw_bytes))
            try:
                stored_path = str(fpath.relative_to(PROJECT_ROOT))
            except ValueError:
                stored_path = str(fpath)
            db.insert_raw_payload(conn, payload_sha256=fr.sha256, source_id_=sid,
                                  first_seen_at=now, byte_size=len(fr.raw_bytes),
                                  file_path=stored_path)

        versions = changes = 0
        for d in dests:
            cid, created = db.resolve_or_create_country(
                conn, sid, run_id, d.destination_id, d.friendly_name, d.long_name or d.name, now)
            if created:
                db.record_change(conn, collection_date=collection_date, fetch_run_id=run_id,
                                 entity="country", country_id=cid, country_name=d.name,
                                 change_type="country_added", changed_fields=None,
                                 old_value=None, new_value=d.name, now=now)
                changes += 1

            derived = derive_malaria(d.malaria)
            chash = derived.content_hash()
            prev = db.current_version(conn, "malaria_record", cid)
            if not (prev and prev["content_hash"] == chash):
                if prev:
                    db.close_version(conn, "malaria_record", prev["record_id"], collection_date)
                fields = _malaria_fields(cid, d, derived, valid_from=collection_date, sid=sid,
                                         run_id=run_id, payload_sha=fr.sha256,
                                         content_hash=chash, now=now)
                new_id = db.insert_version(conn, "malaria_record", fields)
                _insert_area_statements(conn, new_id, derived)
                versions += 1
                if prev:
                    prev_endemic = bool(prev["is_endemic"])
                    if prev_endemic != derived.is_endemic:
                        ct = "became_endemic" if derived.is_endemic else "became_non_endemic"
                        db.record_change(conn, collection_date=collection_date, fetch_run_id=run_id,
                                         entity="malaria", country_id=cid, country_name=d.name,
                                         change_type=ct, changed_fields=None,
                                         old_value=prev["screening_class"],
                                         new_value=derived.screening_class, now=now)
                        changes += 1
                    fields_changed = _diff_malaria(prev, derived)
                    if fields_changed:
                        db.record_change(conn, collection_date=collection_date, fetch_run_id=run_id,
                                         entity="malaria", country_id=cid, country_name=d.name,
                                         change_type="field_changed", changed_fields=fields_changed,
                                         old_value=prev["area_summary"],
                                         new_value=derived.area_summary, now=now)
                        changes += 1

            # Yellow fever (secondary, same SCD2 mechanics)
            yfd = derive_yellowfever(d.yellow_fever)
            yhash = yfd.content_hash(d.yellow_fever.requirements, d.yellow_fever.recommendations)
            yprev = db.current_version(conn, "yellowfever_record", cid)
            if not (yprev and yprev["content_hash"] == yhash):
                if yprev:
                    db.close_version(conn, "yellowfever_record", yprev["record_id"], collection_date)
                db.insert_version(conn, "yellowfever_record", {
                    "country_id": cid, "valid_from": collection_date, "valid_to": None,
                    "is_current": 1, "source_id": sid, "fetch_run_id": run_id,
                    "payload_sha256": fr.sha256, "content_hash": yhash,
                    "has_requirements": None if d.yellow_fever.has_requirements is None
                                        else int(d.yellow_fever.has_requirements),
                    "requirements_html": d.yellow_fever.requirements,
                    "has_recommendations": None if d.yellow_fever.has_recommendations is None
                                           else int(d.yellow_fever.has_recommendations),
                    "recommendations_html": d.yellow_fever.recommendations,
                    "created_at": now,
                })

        # Removals: current malaria records whose country was not seen this run.
        seen = db.country_ids_seen_since(conn, sid, run_id)
        for cid in db.all_current_country_ids(conn, "malaria_record") - seen:
            cur = db.current_version(conn, "malaria_record", cid)
            name_row = conn.execute("SELECT display_name FROM country WHERE country_id=?",
                                    (cid,)).fetchone()
            if cur:
                db.close_version(conn, "malaria_record", cur["record_id"], collection_date)
            ycur = db.current_version(conn, "yellowfever_record", cid)
            if ycur:
                db.close_version(conn, "yellowfever_record", ycur["record_id"], collection_date)
            db.record_change(conn, collection_date=collection_date, fetch_run_id=run_id,
                             entity="country", country_id=cid,
                             country_name=name_row["display_name"] if name_row else None,
                             change_type="country_removed", changed_fields=None,
                             old_value=name_row["display_name"] if name_row else None,
                             new_value=None, now=now)
            changes += 1

        status = "success" if (versions or changes) else "unchanged"
        db.finish_run(conn, run_id, status=status, http_status=200, payload_sha256=fr.sha256,
                      destination_count=len(dests), versions_written=versions,
                      changes_emitted=changes, finished_at=_now())
        db.rebuild_malaria_fts(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        db.finish_run(conn, run_id, status="parse_error", finished_at=_now())
        raise

    return {"status": status, "destinations": len(dests), "versions_written": versions,
            "changes_emitted": changes, "run_id": run_id}


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect CDC malaria data into the versioned SQLite DB.")
    ap.add_argument("--db", default=str(PROJECT_ROOT / "data" / "malaria.db"))
    ap.add_argument("--url", default=cdc_travel.DEFAULT_URL)
    ap.add_argument("--fixture", help="Read payload from a local file instead of fetching.")
    ap.add_argument("--date", help="Override collection_date (YYYY-MM-DD).")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = db_path.parent / "raw" / "cdc_yellowfever_json"
    conn = db.connect(db_path)
    init_and_seed(conn)

    if args.fixture:
        raw_bytes = Path(args.fixture).read_bytes()
        result = cdc_travel.FetchResult(
            raw_bytes=raw_bytes, sha256=hashlib.sha256(raw_bytes).hexdigest(),
            http_status=200,
            destinations=cdc_travel.parse_payload(raw_bytes.decode("utf-8", errors="replace")),
        )
    else:
        result = cdc_travel.fetch(args.url)

    summary = run_collection(conn, result, collection_date=args.date or _today(),
                             now=_now(), raw_dir=raw_dir)
    conn.close()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
