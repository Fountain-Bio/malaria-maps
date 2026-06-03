"""Collector tests: idempotency, SCD2 versioning, change feed, removals, as-of queries."""

import hashlib
import json

from malaria_tracker import collect, db
from malaria_tracker.models import CdcDestination
from malaria_tracker.sources.cdc_travel import FetchResult


def _dest(name, slug, did, *, transmission, area, prophylaxis):
    return {
        "DestinationId": did, "Name": name, "LongName": name, "FriendlyName": slug,
        "Malaria": {"HasTransmission": transmission, "AreaOfRisk": area,
                    "RecommendedProphylaxis": prophylaxis, "Species": "<ul><li><em>P. vivax</em></li></ul>",
                    "ChloroquineResistance": "<ul><li>Chloroquine</li></ul>"},
        "YellowFever": {"HasRequirements": False, "HasRecommendations": False},
    }


def _result(dests):
    raw = json.dumps(dests).encode("utf-8")
    return FetchResult(raw_bytes=raw, sha256=hashlib.sha256(raw).hexdigest(), http_status=200,
                       destinations=[CdcDestination.model_validate(d) for d in dests])


AFG = _dest("Afghanistan", "afghanistan", 118, transmission=True,
            area="<ul><li>All areas &lt;2,500 m elevation</li></ul>",
            prophylaxis="<ul><li>Atovaquone-proguanil, doxycycline</li></ul>")
BRA = _dest("Brazil", "brazil", 2, transmission=True,
            area="<ul><li>Amazon basin states</li><li>No malaria transmission in Rio de Janeiro</li></ul>",
            prophylaxis="<ul><li>Amazon basin: Atovaquone-proguanil, doxycycline</li></ul>")


def _run(conn, dests, date, tmp_path):
    return collect.run_collection(conn, _result(dests), collection_date=date,
                                  now=f"{date}T00:00:00Z", raw_dir=tmp_path / "raw", dest_floor=1)


def test_first_run_and_idempotency(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    collect.init_and_seed(conn)
    r1 = _run(conn, [AFG, BRA], "2026-01-01", tmp_path)
    assert r1["versions_written"] == 2
    assert r1["changes_emitted"] == 2  # two country_added
    assert conn.execute("SELECT COUNT(*) FROM malaria_record WHERE is_current=1").fetchone()[0] == 2

    r2 = _run(conn, [AFG, BRA], "2026-01-01", tmp_path)
    assert r2["status"] == "noop"
    assert conn.execute("SELECT COUNT(*) FROM malaria_record").fetchone()[0] == 2  # no new versions


def test_change_detection_and_as_of(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    collect.init_and_seed(conn)
    _run(conn, [AFG, BRA], "2026-01-01", tmp_path)

    # Afghanistan gains an exclusion -> whole_country becomes partial.
    afg2 = _dest("Afghanistan", "afghanistan", 118, transmission=True,
                 area="<ul><li>All areas &lt;2,500 m elevation</li>"
                      "<li>No malaria transmission in Kabul</li></ul>",
                 prophylaxis="<ul><li>Atovaquone-proguanil, doxycycline</li></ul>")
    r3 = _run(conn, [afg2, BRA], "2026-02-01", tmp_path)
    assert r3["versions_written"] == 1

    events = conn.execute(
        "SELECT change_type FROM change_event WHERE collection_date='2026-02-01'").fetchall()
    assert any(e["change_type"] == "field_changed" for e in events)

    # The original Afghanistan version is closed.
    rows = conn.execute(
        "SELECT valid_from, valid_to, is_current, screening_class FROM malaria_record mr "
        "JOIN country c USING(country_id) WHERE c.display_name='Afghanistan' ORDER BY valid_from"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["valid_to"] == "2026-02-01" and rows[0]["is_current"] == 0
    assert rows[1]["is_current"] == 1

    # As-of mid-January returns the pre-change (whole_country) classification.
    asof = conn.execute(
        "SELECT screening_class FROM malaria_record mr JOIN country c USING(country_id) "
        "WHERE c.display_name='Afghanistan' AND valid_from<=? AND (valid_to>? OR valid_to IS NULL)",
        ("2026-01-15", "2026-01-15")).fetchone()
    assert asof["screening_class"] == "whole_country"
    assert rows[1]["screening_class"] == "partial"


def test_removal(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    collect.init_and_seed(conn)
    _run(conn, [AFG, BRA], "2026-01-01", tmp_path)
    _run(conn, [AFG], "2026-03-01", tmp_path)  # Brazil dropped

    removed = conn.execute(
        "SELECT country_name FROM change_event WHERE change_type='country_removed'").fetchall()
    assert [r["country_name"] for r in removed] == ["Brazil"]
    cur = conn.execute(
        "SELECT COUNT(*) FROM malaria_record mr JOIN country c USING(country_id) "
        "WHERE c.display_name='Brazil' AND is_current=1").fetchone()[0]
    assert cur == 0
