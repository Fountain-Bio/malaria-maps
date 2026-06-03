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


def geojson_lookup() -> dict[str, str]:
    g = json.loads((ROOT / "web" / "world.geojson").read_text())
    lookup: dict[str, str] = {}
    for f in g["features"]:
        p = f["properties"]
        iso = p.get("ISO_A3")
        if not iso or iso == "-99":
            iso = p.get("ISO_A3_EH")
        if not iso or iso == "-99":
            continue
        for key in ("ADMIN", "NAME", "NAME_LONG", "BRK_NAME", "NAME_SORT", "FORMAL_EN", "GEOUNIT"):
            v = p.get(key)
            if v:
                lookup.setdefault(norm(v), iso)
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
            iso, note = OVERRIDES[slug], "override"
        else:
            iso = lookup.get(norm(name)) or lookup.get(norm(slug.replace("-", " ")))
            note = "geojson" if iso else "UNMATCHED"
        if not iso:
            unmatched.append(slug)
        out.append({"friendly_name": slug, "cdc_name": name, "iso3": iso or "", "note": note})

    dst = ROOT / "reference" / "country_iso3.csv"
    with dst.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["friendly_name", "cdc_name", "iso3", "note"])
        w.writeheader()
        w.writerows(out)
    print(f"wrote {dst} ({len(out)} rows, {len(unmatched)} unmatched)")
    if unmatched:
        print("UNMATCHED:", unmatched)


if __name__ == "__main__":
    main()
