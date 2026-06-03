"""Tests for the city-deferral determination and the geocoder ranking (no network)."""

from malaria_tracker import geocode
from malaria_tracker.locate import determine


def geo(name, *, admin1=None, country="X", iso2="XX", elev=None):
    return {"name": name, "admin1": admin1, "admin2": None, "country_name": country,
            "country_iso2": iso2, "lat": 0.0, "lng": 0.0, "elevation_m": elev}


def rec(cls, *, endemic, prophylaxis_html="<ul><li>doxycycline</li></ul>", name="Country"):
    return {"record_id": 1, "display_name": name, "screening_class": cls,
            "is_endemic": 1 if endemic else 0, "area_of_risk_html": "<ul><li>...</li></ul>",
            "recommended_prophylaxis_html": prophylaxis_html}


def area(raw, *, polarity="include", tier="prophylaxis", scope="area",
         place=None, emax=None, emin=None, season=None):
    return {"seq": 0, "raw_text": raw, "polarity": polarity, "tier": tier, "scope": scope,
            "place_name": place, "elev_max_m": emax, "elev_min_m": emin, "season_text": season}


# --------------------------------------------------------------------------- determine
def test_whole_country_within_elevation_defers():
    v = determine("Kabul", geo("Kabul", elev=1800),
                  rec("whole_country", endemic=True),
                  [area("All areas <2,500 m elevation", scope="all", emax=2500, season="April–December")])
    assert v.residence_deferral and v.travel_deferral == "yes" and v.confidence == "high"


def test_whole_country_above_elevation_not_deferred():
    v = determine("Hightown", geo("Hightown", elev=3000),
                  rec("whole_country", endemic=True),
                  [area("All areas <2,500 m elevation", scope="all", emax=2500)])
    assert v.travel_deferral == "no" and "above" in v.travel_reason.lower()
    assert v.residence_deferral  # residence is still country-level


def test_none_not_deferred():
    v = determine("Reykjavik", geo("Reykjavik", country="Iceland"),
                  rec("none", endemic=False), [])
    assert not v.residence_deferral and v.travel_deferral == "no" and v.confidence == "high"


def test_partial_excluded_city():
    v = determine("Cayenne", geo("Cayenne"),
                  rec("partial", endemic=True),  # no carve-out
                  [area("Communes near the border", tier="prophylaxis"),
                   area("No malaria transmission in Cayenne City (the capital)",
                        polarity="exclude", tier="none", place="Cayenne City (the capital)")])
    assert v.travel_deferral == "no" and v.confidence == "high"


def test_partial_region_match_with_carveout():
    proph = ("<ul><li>Campeche, Chiapas: doxycycline</li>"
             "<li>All other areas: No chemoprophylaxis recommended</li></ul>")
    v = determine("Tuxtla", geo("Tuxtla", admin1="Chiapas"),
                  rec("partial", endemic=True, prophylaxis_html=proph),
                  [area("Campeche, Chiapas, and the southern part of Chihuahua state", tier="prophylaxis"),
                   area("No malaria transmission along the U.S.–Mexico border",
                        polarity="exclude", tier="none", place="along the u s mexico border")])
    assert v.travel_deferral == "yes"


def test_partial_unplaceable_with_carveout_is_uncertain_not_no():
    proph = ("<ul><li>Campeche, Chiapas: doxycycline</li>"
             "<li>All other areas: No chemoprophylaxis recommended</li></ul>")
    v = determine("Cancun", geo("Cancun", admin1="Quintana Roo"),
                  rec("partial", endemic=True, prophylaxis_html=proph),
                  [area("Campeche, Chiapas, and the southern part of Chihuahua state", tier="prophylaxis")])
    assert v.travel_deferral == "uncertain"        # safety bias: never a false "no"
    assert v.residence_deferral


def test_partial_no_carveout_within_risk_defers():
    v = determine("Mumbai", geo("Mumbai", admin1="Maharashtra"),
                  rec("partial", endemic=True),     # drugs only, no carve-out
                  [area("Throughout the country including Mumbai", scope="all")])
    assert v.travel_deferral == "yes"


def test_not_in_dataset():
    v = determine("Nowhere", geo("Nowhere"), None, [])
    assert not v.residence_deferral and v.travel_deferral == "no" and not v.in_dataset


def test_india_elevation_exclude_does_not_false_no_for_low_city():
    # A partial, no-carveout country (e.g. India): a scope="all" include plus an exclude that
    # carries BOTH an elev_min and a place= list of example high-altitude states. A low-elevation
    # city whose admin1 is one of those states must NOT be a false "no" — the place-token exclude
    # is ignored because it also carries an elev_min, so only the elevation loop can fire "no".
    areas = [
        area("Throughout the country", scope="all"),
        area("No malaria transmission above 2,000 m in Arunachal Pradesh, Sikkim",
             polarity="exclude", tier="none", place="Arunachal Pradesh, Sikkim", emin=2000),
    ]
    low = determine("Itanagar", geo("Itanagar", admin1="Arunachal Pradesh", elev=300),
                    rec("partial", endemic=True), areas)
    assert low.travel_deferral != "no" and low.travel_deferral == "yes"


def test_india_elevation_exclude_fires_no_above_limit():
    # Same India-shaped data: a city in one of the named states but at/above the elev_min is a
    # genuine no-transmission zone, handled by the elevation loop.
    areas = [
        area("Throughout the country", scope="all"),
        area("No malaria transmission above 2,000 m in Arunachal Pradesh, Sikkim",
             polarity="exclude", tier="none", place="Arunachal Pradesh, Sikkim", emin=2000),
    ]
    high = determine("Gangtok", geo("Gangtok", admin1="Sikkim", elev=2500),
                     rec("partial", endemic=True), areas)
    assert high.travel_deferral == "no"


def test_whole_country_unknown_elevation_is_uncertain():
    # SRTM-failure path: an elevation-gated whole-country rule with no elevation resolved.
    v = determine("Kabul", geo("Kabul", elev=None),
                  rec("whole_country", endemic=True),
                  [area("All areas <2,500 m", scope="all", emax=2500)])
    assert v.travel_deferral == "uncertain" and v.confidence == "low"


def test_partial_no_carveout_all_include_unknown_elevation_is_uncertain():
    v = determine("Kabul", geo("Kabul", elev=None),
                  rec("partial", endemic=True),
                  [area("All areas <2,500 m", scope="all", emax=2500)])
    assert v.travel_deferral == "uncertain" and v.confidence == "low"


def test_partial_place_in_both_include_and_exclude_is_uncertain():
    v = determine("Foo", geo("Foo", admin1="Foo Province"),
                  rec("partial", endemic=True),
                  [area("Foo Province risk", scope="area"),
                   area("No malaria transmission in Foo Province",
                        polarity="exclude", tier="none", place="Foo Province")])
    assert v.travel_deferral == "uncertain"


def test_partial_elevation_exclusion_fires_no():
    # An elevation-based exclude (no place match needed) excludes a high city.
    v = determine("Highville", geo("Highville", elev=2500),
                  rec("partial", endemic=True),
                  [area("No transmission above 2,000 m", polarity="exclude", tier="none", emin=2000)])
    assert v.travel_deferral == "no" and "above" in v.travel_reason.lower()


def test_carveout_sporadic_only_match_is_uncertain():
    proph = ("<ul><li>Campeche: doxycycline</li>"
             "<li>All other areas: No chemoprophylaxis recommended</li></ul>")
    v = determine("Foo", geo("Foo", admin1="Foo Province"),
                  rec("partial", endemic=True, prophylaxis_html=proph),
                  [area("Rare cases or sporadic foci in Foo Province", tier="sporadic")])
    assert v.travel_deferral == "uncertain"


# --------------------------------------------------------------------------- geocoder
def test_candidate_parsing():
    c = geocode._candidate({
        "geonameId": 1, "toponymName": "Cancún", "countryName": "Mexico",
        "countryCode": "MX", "adminName1": "Quintana Roo", "lat": "21.17", "lng": "-86.85",
        "population": 888797,
    })
    assert c.country_iso2 == "MX" and c.admin1 == "Quintana Roo" and c.population == 888797


def test_rank_promotes_exact_name_over_population():
    changchun = geocode.GeoCandidate(2, "Changchun", "China", "CN", "Jilin", None, 0, 0, 4_700_000)
    cancun = geocode.GeoCandidate(1, "Cancún", "Mexico", "MX", "Quintana Roo", None, 0, 0, 888_797)
    ranked = geocode._rank([changchun, cancun], "Cancun")
    assert ranked[0].name == "Cancún"
