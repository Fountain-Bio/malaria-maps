"""Decide whether a geocoded place falls in a malaria deferral geography.

Authority is CDC (our `malaria_record` + `area_statement` rows). GeoNames only *places*
the city; it never decides endemicity. Two verdicts are returned because the FDA rules
differ: residence (>5 yr) is country-level, travel (>24 h, <5 yr) is area-level.

Safety bias: a false "not deferred" is the dangerous error, so when a city in an endemic
country cannot be confidently placed, the travel verdict is `uncertain` (prompting human
review), never a silent `no`. The verbatim CDC text is always returned.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Columns pulled per country; shared by the CLI (sync) and the Datasette plugin (async).
RECORD_SQL = (
    "SELECT record_id, display_name, iso3, iso2, screening_class, is_endemic, "
    "whole_country_endemic, area_of_risk_html, recommended_prophylaxis_html, "
    "species_html, chloroquine_resistant, cdc_updated_date "
    "FROM v_malaria_current WHERE iso2 = ?"
)
AREA_SQL = (
    "SELECT seq, raw_text, polarity, tier, scope, place_name, elev_max_m, elev_min_m, "
    "season_text FROM area_statement WHERE record_id = ? ORDER BY seq"
)

_STOP = {
    "the", "and", "of", "in", "to", "or", "a", "all", "no", "areas", "area", "city",
    "state", "states", "province", "provinces", "district", "districts", "subdistricts",
    "region", "regions", "county", "including", "its", "capital", "part", "parts",
    "southern", "northern", "eastern", "western", "central", "near", "border", "borders",
    "with", "other", "rare", "cases", "sporadic", "foci", "transmission", "malaria",
    "elevation", "below", "above", "during", "associated", "primarily", "less", "commonly",
}


def _norm(s: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split())


def _tokens(*parts: str | None) -> set[str]:
    out: set[str] = set()
    for p in parts:
        out |= {t for t in _norm(p).split() if len(t) >= 4 and t not in _STOP}
    return out


@dataclass
class Verdict:
    query: str
    resolved: dict
    in_dataset: bool
    screening_class: str | None
    residence_deferral: bool
    travel_deferral: str            # 'yes' | 'no' | 'uncertain'
    travel_reason: str
    matched_statement: str | None
    confidence: str                 # 'high' | 'medium' | 'low'
    season_note: str | None
    verbatim_area_html: str | None
    # Reference fields (so the city card shows the same detail as the country panel).
    species_html: str | None = None
    prophylaxis_html: str | None = None
    chloroquine_resistant: int | None = None
    cdc_updated_date: str | None = None
    display_name: str | None = None
    citation: str = ("FDA 12/2022; endemic = where CDC recommends chemoprophylaxis. "
                     "A Jan-2025 FDA draft would move to selective testing.")
    alternates: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def attach_record_fields(verdict: Verdict, record: dict | None) -> Verdict:
    """Copy the CDC reference fields from the record onto the verdict (in place)."""
    if record:
        verdict.species_html = record.get("species_html")
        verdict.prophylaxis_html = record.get("recommended_prophylaxis_html")
        verdict.chloroquine_resistant = record.get("chloroquine_resistant")
        verdict.cdc_updated_date = record.get("cdc_updated_date")
        verdict.display_name = record.get("display_name")
    return verdict


def _place_matches(stmt: dict, place_tokens: set[str]) -> bool:
    """True if the city/admin tokens overlap the statement's discriminating tokens."""
    target = stmt.get("place_name") or stmt.get("raw_text")
    return bool(place_tokens & _tokens(target))


def _within_elevation(stmt: dict, elev: int | None) -> str:
    """'within' | 'above' | 'unknown' for an include statement carrying an elev_max."""
    emax = stmt.get("elev_max_m")
    if not emax:
        return "within"          # no elevation gate -> the whole area counts
    if elev is None:
        return "unknown"
    return "within" if elev <= emax else "above"


def determine(query: str, geo: dict, record: dict | None, area_statements: list[dict]) -> Verdict:
    resolved = {
        "name": geo.get("name"), "admin1": geo.get("admin1"), "admin2": geo.get("admin2"),
        "country_name": geo.get("country_name"), "country_iso2": geo.get("country_iso2"),
        "lat": geo.get("lat"), "lng": geo.get("lng"), "elevation_m": geo.get("elevation_m"),
    }

    if record is None:
        return Verdict(query, resolved, False, None, False, "no",
                       "Not a CDC malaria travel destination; no malaria deferral applies.",
                       None, "medium", None, None)

    cls = record["screening_class"]
    endemic = bool(record["is_endemic"])
    verbatim = record.get("area_of_risk_html")
    residence = endemic

    if cls == "none":
        return Verdict(query, resolved, True, cls, False, "no",
                       f"CDC recommends no chemoprophylaxis in {record['display_name']}, "
                       "so neither residence nor travel triggers malaria deferral.",
                       None, "high", None, verbatim)

    elev = geo.get("elevation_m")
    place_toks = _tokens(geo.get("name"), geo.get("admin1"), geo.get("admin2"))
    includes = [a for a in area_statements if a["polarity"] == "include"]
    excludes = [a for a in area_statements if a["polarity"] == "exclude"]
    season = next((a["season_text"] for a in includes if a.get("season_text")), None)

    if cls == "whole_country":
        gate = next((a for a in includes if a["scope"] == "all" and a.get("elev_max_m")), None)
        if gate:
            band = _within_elevation(gate, elev)
            if band == "above":
                return Verdict(query, resolved, True, cls, residence, "no",
                               f"{geo.get('name')} at {elev} m is above the {gate['elev_max_m']} m "
                               "malaria elevation limit.", gate["raw_text"], "high", season, verbatim)
            if band == "unknown":
                return Verdict(query, resolved, True, cls, residence, "uncertain",
                               f"Risk depends on the {gate['elev_max_m']} m elevation limit and the "
                               "elevation could not be determined.", gate["raw_text"], "low", season, verbatim)
        return Verdict(query, resolved, True, cls, residence, "yes",
                       "Whole-country malaria risk: travel anywhere triggers deferral.",
                       (gate or {}).get("raw_text"), "high", season, verbatim)

    # ---- partial ----
    has_carveout = "no chemoprophylaxis" in (record.get("recommended_prophylaxis_html") or "").lower()

    # Elevation-based exclusions (e.g. "no transmission above 2,000 m").
    for a in excludes:
        emin = a.get("elev_min_m")
        if emin and elev is not None and elev >= emin:
            return Verdict(query, resolved, True, cls, residence, "no",
                           f"{geo.get('name')} at {elev} m is above {emin} m, a no-transmission zone.",
                           a["raw_text"], "high", season, verbatim)

    exclude_hit = next((a for a in excludes if a.get("place_name") and _place_matches(a, place_toks)), None)
    proph_inc = [a for a in includes if a["tier"] == "prophylaxis"]
    sporadic_inc = [a for a in includes if a["tier"] == "sporadic"]
    include_hit = next((a for a in includes if a["scope"] != "all" and _place_matches(a, place_toks)), None)
    all_include = next((a for a in includes if a["scope"] == "all"), None)

    if exclude_hit and not include_hit:
        return Verdict(query, resolved, True, cls, residence, "no",
                       f"CDC lists this location as a no-transmission area: \"{exclude_hit['raw_text']}\".",
                       exclude_hit["raw_text"], "high", season, verbatim)
    if exclude_hit and include_hit:
        return Verdict(query, resolved, True, cls, residence, "uncertain",
                       "This place appears in both a risk and a no-transmission description; "
                       "review the CDC text.", exclude_hit["raw_text"], "low", season, verbatim)

    if not has_carveout:
        # Chemoprophylaxis is recommended across the country's risk areas.
        gate = all_include if (all_include and all_include.get("elev_max_m")) else None
        if gate:
            band = _within_elevation(gate, elev)
            if band == "above":
                return Verdict(query, resolved, True, cls, residence, "no",
                               f"{geo.get('name')} at {elev} m is above the {gate['elev_max_m']} m "
                               "malaria elevation limit.", gate["raw_text"], "high", season, verbatim)
            if band == "unknown":
                return Verdict(query, resolved, True, cls, residence, "uncertain",
                               f"Risk depends on the {gate['elev_max_m']} m elevation limit; elevation unknown.",
                               gate["raw_text"], "low", season, verbatim)
        if include_hit or all_include:
            return Verdict(query, resolved, True, cls, residence, "yes",
                           "Within the country's malaria risk area, where CDC recommends chemoprophylaxis.",
                           (include_hit or all_include)["raw_text"], "medium", season, verbatim)
        return Verdict(query, resolved, True, cls, residence, "uncertain",
                       "Could not place this location within the listed risk areas; review the CDC text.",
                       None, "low", season, verbatim)

    # has_carveout: must match a chemoprophylaxis-recommended area specifically.
    proph_hit = include_hit if include_hit in proph_inc else next(
        (a for a in proph_inc if a["scope"] == "all" or _place_matches(a, place_toks)), None)
    if proph_hit:
        return Verdict(query, resolved, True, cls, residence, "yes",
                       "Within a CDC chemoprophylaxis-recommended area.",
                       proph_hit["raw_text"], "medium", season, verbatim)
    sporadic_hit = next((a for a in sporadic_inc if _place_matches(a, place_toks)), None)
    if sporadic_hit:
        return Verdict(query, resolved, True, cls, residence, "uncertain",
                       "Falls in an area of rare/sporadic transmission; CDC may not recommend "
                       "chemoprophylaxis there. Review the CDC text.",
                       sporadic_hit["raw_text"], "low", season, verbatim)
    return Verdict(query, resolved, True, cls, residence, "uncertain",
                   "Could not place this location within the chemoprophylaxis-recommended areas; "
                   "review the CDC text.", None, "low", season, verbatim)


# --------------------------------------------------------------------------- DB load (sync)
def load_record(conn, iso2: str) -> tuple[dict | None, list[dict]]:
    rec = conn.execute(RECORD_SQL, (iso2,)).fetchone()
    if not rec:
        return None, []
    rec = dict(rec)
    areas = [dict(a) for a in conn.execute(AREA_SQL, (rec["record_id"],)).fetchall()]
    return rec, areas


def needs_elevation(area_statements: list[dict]) -> bool:
    """Whether any area rule depends on elevation, so the SRTM call is worth making."""
    return any(a.get("elev_max_m") or a.get("elev_min_m") for a in area_statements)


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    from pathlib import Path

    from . import db, geocode

    ap = argparse.ArgumentParser(description="Is a city in a malaria deferral geography?")
    ap.add_argument("query")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[2] / "data" / "malaria.db"))
    args = ap.parse_args(argv)

    cands = geocode.search_place(args.query)
    if not cands:
        print(json.dumps({"error": "no geocode match", "query": args.query}))
        return 1
    chosen, alternates = cands[0], cands[1:5]
    conn = db.connect(args.db)
    rec, areas = load_record(conn, chosen.country_iso2)
    elev = geocode.fetch_elevation(chosen.lat, chosen.lng) if needs_elevation(areas) else None
    geo = {**chosen.to_dict(), "elevation_m": elev}
    verdict = determine(args.query, geo, rec, areas)
    attach_record_fields(verdict, rec)
    verdict.alternates = [c.to_dict() for c in alternates]
    conn.close()
    print(json.dumps(verdict.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
