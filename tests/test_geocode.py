"""Geocoder tests with the single network chokepoint (geocode._get) fully mocked.

No real GeoNames calls are made. The persistent sqlite cache lives in a tmp file, and the
lru caches are cleared per test, so each scenario exercises a clean fetch path.
"""

import pytest

from malaria_tracker import geocode


@pytest.fixture
def gc(tmp_path, monkeypatch):
    """Isolate the geocoder: tmp cache file, cleared lru caches, a username present."""
    monkeypatch.setattr(geocode, "CACHE_PATH", tmp_path / "gc.sqlite")
    monkeypatch.setattr(geocode, "_cache_ready", False)
    geocode.search_place.cache_clear()
    geocode.fetch_elevation.cache_clear()
    geocode.lookup_geoname.cache_clear()
    monkeypatch.setenv("GEONAMES_USERNAME", "test")
    yield geocode
    geocode.search_place.cache_clear()
    geocode.fetch_elevation.cache_clear()
    geocode.lookup_geoname.cache_clear()


def _candidate_json(name="Testville", **over):
    g = {
        "geonameId": 123, "toponymName": name, "countryName": "Testland",
        "countryCode": "TL", "adminName1": "Test Province", "adminName2": None,
        "lat": "1.5", "lng": "2.5", "population": 1000,
    }
    g.update(over)
    return g


def test_strict_then_fuzzy_fallback(gc, monkeypatch):
    candidate = _candidate_json(name="Fuzzytown")

    def fake_get(path, params, client):
        if "isNameRequired" in params:
            return {"geonames": []}            # strict pass finds nothing
        if "fuzzy" in params:
            return {"geonames": [candidate]}   # fuzzy pass finds the place
        raise AssertionError(f"unexpected params: {params}")

    monkeypatch.setattr(gc, "_get", fake_get)
    cands = gc.search_place("X")
    assert len(cands) == 1
    assert cands[0].name == "Fuzzytown" and cands[0].country_iso2 == "TL"


def test_persistent_cache_round_trip(gc, monkeypatch):
    candidate = _candidate_json(name="Cachetown")

    def fake_get(path, params, client):
        return {"geonames": [candidate]}

    monkeypatch.setattr(gc, "_get", fake_get)
    first = gc.search_place("Y")
    assert first[0].name == "Cachetown"

    # Drop the lru cache only; the sqlite cache must still satisfy the lookup with no _get call.
    def boom(*a, **k):
        raise AssertionError("_get should not be called; result must come from sqlite cache")

    monkeypatch.setattr(gc, "_get", boom)
    gc.search_place.cache_clear()
    again = gc.search_place("Y")
    assert again[0].name == "Cachetown" and again[0].geoname_id == first[0].geoname_id


def test_elevation_sentinel_returns_none(gc, monkeypatch):
    def fake_get(path, params, client):
        return {"srtm3": -32768}              # SRTM unknown / outside coverage

    monkeypatch.setattr(gc, "_get", fake_get)
    assert gc.fetch_elevation(1.0, 2.0) is None

    # The sentinel is cached as -32768 and must read back as None without another _get call.
    def boom(*a, **k):
        raise AssertionError("_get should not be called; sentinel must come from cache")

    monkeypatch.setattr(gc, "_get", boom)
    gc.fetch_elevation.cache_clear()
    assert gc.fetch_elevation(1.0, 2.0) is None


def test_geonames_error_raised(gc, monkeypatch):
    def fake_get(path, params, client):
        return {"status": {"value": 18, "message": "daily limit exceeded"}}

    monkeypatch.setattr(gc, "_get", fake_get)
    with pytest.raises(gc.GeoNamesError):
        gc.search_place("Z")
