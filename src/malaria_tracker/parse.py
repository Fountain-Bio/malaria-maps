"""Parse the CDC malaria HTML fragments into a normalized, screening-relevant model.

Ground truth (real CDC payload):
  Afghanistan AreaOfRisk: "All areas <2,500 m (<8,200 ft) elevation (April-December)"
  French Guiana: includes named communes; "No malaria transmission in Cayenne City"
  Botswana RecommendedProphylaxis: districts get drugs; "Areas with rare cases or sporadic
    foci of transmission: no chemoprophylaxis recommended (... mosquito avoidance only)"
  Mexico: specific states get drugs; "All other areas with malaria transmission: No
    chemoprophylaxis recommended ..."

The authoritative deferral signal is RecommendedProphylaxis: a place is endemic-for-deferral
where a drug is recommended, NOT merely where HasTransmission is true. So
`country_has_any_prophylaxis_area` is derived from the prophylaxis field, and the AreaOfRisk
breakdown is annotated as a best-effort geographic tiering.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from .models import AreaStatement, CdcMalaria, MalariaDerived, YellowFeverDerived, CdcYellowFever

KNOWN_DRUGS = [
    "atovaquone-proguanil", "chloroquine", "doxycycline",
    "mefloquine", "primaquine", "tafenoquine",
]
KNOWN_SPECIES = ["falciparum", "vivax", "malariae", "ovale", "knowlesi"]
MONTHS = ("january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december")
NO_PROPHYLAXIS = "no chemoprophylaxis recommended"


def strip_html(fragment: str | None) -> str:
    if not fragment:
        return ""
    return HTMLParser(fragment).text(separator=" ").strip()


def list_items(fragment: str | None) -> list[str]:
    """Return the text of each <li>; fall back to the whole stripped text if no list."""
    if not fragment:
        return []
    tree = HTMLParser(fragment)
    items = [li.text(separator=" ").strip() for li in tree.css("li")]
    items = [" ".join(i.split()) for i in items if i.strip()]
    if items:
        return items
    whole = strip_html(fragment)
    return [whole] if whole else []


# --------------------------------------------------------------------------- field parsers
def parse_species(fragment: str | None) -> list[str]:
    text = strip_html(fragment).lower()
    found = {f"P. {s}" for s in KNOWN_SPECIES if re.search(rf"\bp\.\s*{s}\b", text)}
    return sorted(found)


def parse_drugs(prophylaxis_fragment: str | None) -> list[str]:
    """Union of drug names across prophylaxis <li>s that actually recommend a drug."""
    drugs: set[str] = set()
    for li in list_items(prophylaxis_fragment):
        low = li.lower()
        if NO_PROPHYLAXIS in low:
            continue
        for d in KNOWN_DRUGS:
            if d in low:
                drugs.add(d)
    return sorted(drugs)


def parse_chloroquine_resistant(fragment: str | None) -> bool | None:
    text = strip_html(fragment).strip().lower()
    if not text:
        return None
    if text in ("none", "no", "n/a"):
        return False
    if "chloroquine" in text:
        return True
    return None


def parse_updated_date(*fragments: str | None) -> str | None:
    """Find 'Updated <Month> <Day>, <Year>' and return ISO 'YYYY-MM-DD'."""
    for frag in fragments:
        if not frag:
            continue
        m = re.search(r"Updated\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", strip_html(frag))
        if m:
            month_name, day, year = m.group(1), int(m.group(2)), int(m.group(3))
            try:
                month = MONTHS.index(month_name.lower()) + 1
            except ValueError:
                continue
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _elevations(text: str) -> tuple[int | None, int | None]:
    low = text.lower()
    emax = emin = None
    m = re.search(r"<\s*([\d,]+)\s*m\b", low)
    if m:
        emax = int(m.group(1).replace(",", ""))
    m = re.search(r">\s*([\d,]+)\s*m\b", low)
    if m:
        emin = int(m.group(1).replace(",", ""))
    return emax, emin


def _season(text: str) -> str | None:
    for m in re.finditer(r"\(([^)]*)\)", text):
        inner = m.group(1)
        if any(mon in inner.lower() for mon in MONTHS):
            return inner.strip()
    return None


def parse_area_statements(area_fragment: str | None) -> list[AreaStatement]:
    out: list[AreaStatement] = []
    for seq, raw in enumerate(list_items(area_fragment)):
        low = raw.lower()
        is_exclude = low.startswith("no malaria transmission") or low.startswith("no transmission")
        if is_exclude:
            polarity, tier = "exclude", "none"
        elif "rare" in low or "sporadic" in low:
            polarity, tier = "include", "sporadic"
        else:
            polarity, tier = "include", "prophylaxis"

        # "All areas <2,500 m" / "Throughout the country" = country-wide; but
        # "All areas in the states of Acre, Amapá…" is region-specific, not country-wide.
        region_specific = re.search(r"\bstates?\s+of\b|\bin\s+the\s+(state|province|department|region|district)", low)
        scope = "all" if re.match(r"^(all\b|throughout\b)", low) and not region_specific else "area"
        emax, emin = _elevations(raw)
        season = _season(raw)

        place = None
        if is_exclude:
            m = re.search(r"\bin\s+(.*)$", raw, flags=re.IGNORECASE)
            if m:
                place = m.group(1).strip().rstrip(".")

        out.append(AreaStatement(
            seq=seq, raw_text=raw, polarity=polarity, tier=tier, scope=scope,
            place_name=place, elev_max_m=emax, elev_min_m=emin, season_text=season,
        ))
    return out


def _summary(statements: list[AreaStatement], has_transmission: bool, has_prophylaxis: bool) -> str | None:
    if not has_transmission:
        return None
    if not has_prophylaxis:
        return "Transmission present; no chemoprophylaxis recommended (mosquito avoidance only)."
    if not statements:
        return None
    includes = [s for s in statements if s.polarity == "include"]
    excludes = [s for s in statements if s.polarity == "exclude"]
    if includes and includes[0].scope == "all" and not excludes:
        return includes[0].raw_text
    parts = []
    if includes:
        parts.append(f"{len(includes)} risk area statement(s)")
    if excludes:
        parts.append(f"{len(excludes)} no-transmission exclusion(s)")
    return "; ".join(parts) if parts else None


# --------------------------------------------------------------------------- top-level derive
def derive_malaria(m: CdcMalaria) -> MalariaDerived:
    statements = parse_area_statements(m.area_of_risk)
    drugs = parse_drugs(m.recommended_prophylaxis)

    # Authoritative deferral signal: does any prophylaxis <li> recommend a drug?
    prophylaxis_lis = list_items(m.recommended_prophylaxis)
    has_prophylaxis_area = any(
        (NO_PROPHYLAXIS not in li.lower()) and any(d in li.lower() for d in KNOWN_DRUGS)
        for li in prophylaxis_lis
    )
    # Fallback: transmission with drugs listed but a single unlabeled li.
    if not has_prophylaxis_area and m.has_transmission and drugs and len(prophylaxis_lis) <= 1:
        has_prophylaxis_area = bool(drugs)

    includes = [s for s in statements if s.polarity == "include"]
    excludes = [s for s in statements if s.polarity == "exclude"]
    has_all = any(s.scope == "all" and s.tier != "sporadic" for s in includes)
    only_prophylaxis_includes = includes and all(s.tier == "prophylaxis" for s in includes)
    whole_country = bool(has_prophylaxis_area and has_all and not excludes and only_prophylaxis_includes)

    if not (m.has_transmission and has_prophylaxis_area):
        screening_class = "none"
    elif whole_country:
        screening_class = "whole_country"
    else:
        screening_class = "partial"

    return MalariaDerived(
        has_transmission=m.has_transmission,
        is_endemic=bool(m.has_transmission and has_prophylaxis_area),
        whole_country_endemic=whole_country,
        country_has_any_prophylaxis_area=has_prophylaxis_area,
        screening_class=screening_class,
        prophylaxis_drugs=drugs,
        species=parse_species(m.species),
        chloroquine_resistant=parse_chloroquine_resistant(m.chloroquine_resistance),
        area_summary=_summary(statements, m.has_transmission, has_prophylaxis_area),
        cdc_updated_date=parse_updated_date(m.recommended_prophylaxis, m.area_of_risk, m.species),
        area_statements=statements,
    )


def derive_yellowfever(y: CdcYellowFever) -> YellowFeverDerived:
    return YellowFeverDerived(
        has_requirements=y.has_requirements, has_recommendations=y.has_recommendations,
    )
