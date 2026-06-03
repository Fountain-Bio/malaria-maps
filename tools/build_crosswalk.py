"""Build reference/country_iso3.csv: CDC FriendlyName slug -> ISO-3166 alpha-3.

Auto-matches CDC destination names against the Natural Earth polygons, then applies a
hand-verified override map for small nations / dependencies the 110m polygons miss or
name differently. Re-runnable; writes the CSV deterministically (sorted by slug).
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Hand-verified ISO3 for entities the auto-match misses (small states + dependencies).
OVERRIDES = {
    "american-samoa": "ASM", "andorra": "AND", "anguilla": "AIA", "antigua-and-barbuda": "ATG",
    "aruba": "ABW", "azores": "PRT", "bahrain": "BHR", "barbados": "BRB", "bermuda": "BMU",
    "bonaire": "BES", "british-indian-ocean-territory": "IOT", "burma": "MMR",
    "canary-islands": "ESP", "cape-verde": "CPV", "cayman-islands": "CYM",
    "christmas-island": "CXR", "cocos-islands": "CCK", "comoros": "COM", "cook-islands": "COK",
    "curacao": "CUW", "dominica": "DMA", "easter-island": "CHL", "faroe-island": "FRO",
    "french-guiana": "GUF", "french-polynesia": "PYF", "gibraltar": "GIB", "grenada": "GRD",
    "guadeloupe": "GLP", "guam": "GUM", "hong-kong-sar": "HKG", "kiribati": "KIR",
    "kosovo": "XKX", "liechtenstein": "LIE", "macau-sar": "MAC", "madeira-islands": "PRT",
    "maldives": "MDV", "malta": "MLT", "marshall-islands": "MHL", "martinique": "MTQ",
    "mauritius": "MUS", "mayotte": "MYT", "micronesia": "FSM", "monaco": "MCO",
    "montserrat": "MSR", "nauru": "NRU", "niue": "NIU", "norfolk-island": "NFK",
    "northern-mariana-islands": "MNP", "palau": "PLW", "pitcairn-islands": "PCN",
    "reunion": "REU", "saba": "BES", "saint-barthelemy": "BLM", "saint-helena": "SHN",
    "st-kitts-and-nevis": "KNA", "saint-lucia": "LCA", "saint-martin": "MAF",
    "saint-pierre-and-miquelon": "SPM", "saint-vincent-and-the-grenadines": "VCT",
    "samoa": "WSM", "san-marino": "SMR", "seychelles": "SYC", "singapore": "SGP",
    "sint-eustatius": "BES", "sint-maarten": "SXM",
    "south-georgia-south-sandwich-islands": "SGS", "sao-tome-and-principe": "STP",
    "tokelau": "TKL", "tonga": "TON", "turks-and-caicos": "TCA", "tuvalu": "TUV",
    "british-virgin-islands": "VGB", "usvirgin-islands": "VIR", "wake-island": "UMI",
}

# ISO-3166 alpha-2 for the same override entities (GeoNames returns alpha-2).
OVERRIDES_ISO2 = {
    "american-samoa": "AS", "andorra": "AD", "anguilla": "AI", "antigua-and-barbuda": "AG",
    "aruba": "AW", "azores": "PT", "bahrain": "BH", "barbados": "BB", "bermuda": "BM",
    "bonaire": "BQ", "british-indian-ocean-territory": "IO", "burma": "MM",
    "canary-islands": "ES", "cape-verde": "CV", "cayman-islands": "KY",
    "christmas-island": "CX", "cocos-islands": "CC", "comoros": "KM", "cook-islands": "CK",
    "curacao": "CW", "dominica": "DM", "easter-island": "CL", "faroe-island": "FO",
    "french-guiana": "GF", "french-polynesia": "PF", "gibraltar": "GI", "grenada": "GD",
    "guadeloupe": "GP", "guam": "GU", "hong-kong-sar": "HK", "kiribati": "KI",
    "kosovo": "XK", "liechtenstein": "LI", "macau-sar": "MO", "madeira-islands": "PT",
    "maldives": "MV", "malta": "MT", "marshall-islands": "MH", "martinique": "MQ",
    "mauritius": "MU", "mayotte": "YT", "micronesia": "FM", "monaco": "MC",
    "montserrat": "MS", "nauru": "NR", "niue": "NU", "norfolk-island": "NF",
    "northern-mariana-islands": "MP", "palau": "PW", "pitcairn-islands": "PN",
    "reunion": "RE", "saba": "BQ", "saint-barthelemy": "BL", "saint-helena": "SH",
    "st-kitts-and-nevis": "KN", "saint-lucia": "LC", "saint-martin": "MF",
    "saint-pierre-and-miquelon": "PM", "saint-vincent-and-the-grenadines": "VC",
    "samoa": "WS", "san-marino": "SM", "seychelles": "SC", "singapore": "SG",
    "sint-eustatius": "BQ", "sint-maarten": "SX",
    "south-georgia-south-sandwich-islands": "GS", "sao-tome-and-principe": "ST",
    "tokelau": "TK", "tonga": "TO", "turks-and-caicos": "TC", "tuvalu": "TV",
    "british-virgin-islands": "VG", "usvirgin-islands": "VI", "wake-island": "UM",
}


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = s.replace("&", " and ").replace(".", " ")
    s = re.sub(r"\bthe\b|\brepublic of\b|\bislands?\b|\bst \b", " ", s)
    s = s.replace("saint", "st")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def geojson_lookup() -> dict[str, tuple[str, str]]:
    g = json.loads((ROOT / "web" / "world.geojson").read_text())
    lookup: dict[str, tuple[str, str]] = {}
    for f in g["features"]:
        p = f["properties"]
        iso3 = p.get("ISO_A3")
        if not iso3 or iso3 == "-99":
            iso3 = p.get("ISO_A3_EH")
        if not iso3 or iso3 == "-99":
            continue
        iso2 = p.get("ISO_A2")
        if not iso2 or iso2 == "-99":
            iso2 = p.get("ISO_A2_EH") or ""
        for key in ("ADMIN", "NAME", "NAME_LONG", "BRK_NAME", "NAME_SORT", "FORMAL_EN", "GEOUNIT"):
            v = p.get(key)
            if v:
                lookup.setdefault(norm(v), (iso3, iso2))
    return lookup


def main() -> None:
    lookup = geojson_lookup()
    conn = sqlite3.connect(ROOT / "data" / "malaria.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.display_name, a.native_key AS slug FROM country c "
        "JOIN country_alias a ON a.country_id=c.country_id AND a.native_kind='cdc_slug' "
        "ORDER BY a.native_key"
    ).fetchall()

    out = []
    unmatched = []
    for r in rows:
        name, slug = r["display_name"], r["slug"]
        if slug in OVERRIDES:
            iso3, iso2, note = OVERRIDES[slug], OVERRIDES_ISO2.get(slug, ""), "override"
        else:
            hit = lookup.get(norm(name)) or lookup.get(norm(slug.replace("-", " ")))
            iso3, iso2 = (hit[0], hit[1]) if hit else ("", "")
            note = "geojson" if iso3 else "UNMATCHED"
        if not iso3:
            unmatched.append(slug)
        out.append({"friendly_name": slug, "cdc_name": name, "iso3": iso3,
                    "iso2": iso2, "note": note})

    dst = ROOT / "reference" / "country_iso3.csv"
    with dst.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["friendly_name", "cdc_name", "iso3", "iso2", "note"])
        w.writeheader()
        w.writerows(out)
    missing_iso2 = [r["friendly_name"] for r in out if r["iso3"] and not r["iso2"]]
    print(f"wrote {dst} ({len(out)} rows, {len(unmatched)} unmatched iso3, "
          f"{len(missing_iso2)} missing iso2)")
    if unmatched:
        print("UNMATCHED iso3:", unmatched)
    if missing_iso2:
        print("MISSING iso2:", missing_iso2)


if __name__ == "__main__":
    main()
