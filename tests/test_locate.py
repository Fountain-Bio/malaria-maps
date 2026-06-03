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


# --------------------------------------------------------------------------- geocoder
def test_candidate_parsing():
    c = geocode._candidate({
        "geonameId": 1, "toponymName": "Cancún", "countryName": "Mexico",
        "countryCode": "MX", "adminName1": "Quintana Roo", "lat": "21.17", "lng": "-86.85",
        "population": 888797, "fcode": "PPLA2",
    })
    assert c.country_iso2 == "MX" and c.admin1 == "Quintana Roo" and c.population == 888797


def test_rank_promotes_exact_name_over_population():
    changchun = geocode.GeoCandidate(2, "Changchun", "China", "CN", "Jilin", None, 0, 0, 4_700_000, "PPLA")
    cancun = geocode.GeoCandidate(1, "Cancún", "Mexico", "MX", "Quintana Roo", None, 0, 0, 888_797, "PPLA2")
    ranked = geocode._rank([changchun, cancun], "Cancun")
    assert ranked[0].name == "Cancún"
