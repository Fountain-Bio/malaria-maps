"""GeoNames geocoder: resolve a place name to country / admin region / elevation / coords.

Free web service, authenticated by username only, CC BY (attribute GeoNames in the UI).
A lookup is two calls: searchJSON (1 credit) + srtm3JSON (0.2 credit). Limits are 10,000
credits/day and 1,000/hour per username, so an in-memory LRU cache is plenty.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

import httpx

from .config import geonames_username

BASE = "http://api.geonames.org"


def _fold(s: str | None) -> str:
    """Lowercase and strip accents, so 'Cancun' folds to match 'Cancún'."""
    return "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                   if not unicodedata.combining(c))


class GeoNamesError(RuntimeError):
    pass


@dataclass
class GeoCandidate:
    geoname_id: int
    name: str
    country_name: str
    country_iso2: str
    admin1: str | None
    admin2: str | None
    lat: float
    lng: float
    population: int
    fcode: str | None

    def to_dict(self) -> dict:
        return {
            "geoname_id": self.geoname_id, "name": self.name,
            "country_name": self.country_name, "country_iso2": self.country_iso2,
            "admin1": self.admin1, "admin2": self.admin2,
            "lat": self.lat, "lng": self.lng, "population": self.population,
        }


@dataclass
class GeoResult:
    chosen: GeoCandidate
    elevation_m: int | None              # None when unknown (-32768 sentinel or out of coverage)
    alternates: list[GeoCandidate] = field(default_factory=list)


def _username(username: str | None) -> str:
    username = username or geonames_username()
    if not username:
        raise GeoNamesError("GEONAMES_USERNAME is not set (add it to .env)")
    return username


def _check_status(data: dict | list) -> None:
    if isinstance(data, dict) and "status" in data:
        st = data["status"]
        raise GeoNamesError(f"GeoNames error {st.get('value')}: {st.get('message', '')}")


def _candidate(g: dict) -> GeoCandidate:
    return GeoCandidate(
        geoname_id=int(g["geonameId"]),
        name=g.get("toponymName") or g.get("name", ""),
        country_name=g.get("countryName", ""),
        country_iso2=g.get("countryCode", ""),
        admin1=g.get("adminName1") or None,
        admin2=g.get("adminName2") or None,
        lat=float(g["lat"]), lng=float(g["lng"]),
        population=int(g.get("population") or 0),
        fcode=g.get("fcode") or None,
    )


def _get(path: str, params: dict, client: httpx.Client) -> dict:
    r = client.get(BASE + path, params=params)
    r.raise_for_status()
    return r.json()


def _rank(cands: list[GeoCandidate], q: str) -> list[GeoCandidate]:
    """Promote exact (accent-folded) name matches above population, so 'Cancun' picks
    Cancún (exact) over Changchun (higher population but a loose match)."""
    qf = _fold(q)
    def key(c: GeoCandidate) -> tuple[int, int]:
        nf = _fold(c.name)
        exact = 2 if nf == qf else (1 if nf.startswith(qf) else 0)
        return (-exact, -c.population)
    return sorted(cands, key=key)


def search(q: str, username: str, client: httpx.Client, max_rows: int = 10) -> list[GeoCandidate]:
    base = {"q": q, "featureClass": "P", "style": "FULL", "orderby": "population",
            "maxRows": max_rows, "username": username}
    # Strict pass: the search term must be part of the place name (no fuzzy).
    data = _get("/searchJSON", {**base, "isNameRequired": "true"}, client)
    _check_status(data)
    cands = [_candidate(g) for g in data.get("geonames", [])]
    if not cands:
        # Fuzzy fallback only when the strict pass finds nothing (tolerates misspellings).
        data = _get("/searchJSON", {**base, "fuzzy": "0.8"}, client)
        _check_status(data)
        cands = [_candidate(g) for g in data.get("geonames", [])]
    return _rank(cands, q)


def by_geoname_id(geoname_id: int, username: str, client: httpx.Client) -> GeoCandidate | None:
    data = _get("/getJSON", {"geonameId": geoname_id, "style": "FULL", "username": username}, client)
    _check_status(data)
    if not data.get("geonameId"):
        return None
    return _candidate(data)


def elevation(lat: float, lng: float, username: str, client: httpx.Client) -> int | None:
    data = _get("/srtm3JSON", {"lat": lat, "lng": lng, "username": username}, client)
    _check_status(data)
    v = data.get("srtm3")
    if v is None or v <= -32000:        # -32768 = unknown / outside SRTM coverage
        return None
    return int(v)


def _resolve(chosen: GeoCandidate, alternates: list[GeoCandidate], username: str,
             client: httpx.Client) -> GeoResult:
    elev = elevation(chosen.lat, chosen.lng, username, client)
    return GeoResult(chosen=chosen, elevation_m=elev, alternates=alternates)


@lru_cache(maxsize=512)
def geocode(q: str, username: str | None = None) -> GeoResult | None:
    """Geocode a place name. Returns the top populated-place match + elevation + alternates."""
    username = _username(username)
    with httpx.Client(timeout=20.0) as client:
        cands = search(q, username, client)
        if not cands:
            return None
        return _resolve(cands[0], cands[1:5], username, client)


def resolve_geoname_id(geoname_id: int, username: str | None = None) -> GeoResult | None:
    """Resolve a specific GeoNames id (used for disambiguation)."""
    username = _username(username)
    with httpx.Client(timeout=20.0) as client:
        c = by_geoname_id(geoname_id, username, client)
        if not c:
            return None
        return _resolve(c, [], username, client)
