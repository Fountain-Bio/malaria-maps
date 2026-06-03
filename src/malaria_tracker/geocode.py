"""GeoNames geocoder with a persistent cache and on-demand elevation.

Free web service, authenticated by username only, CC BY (attribute GeoNames). To stay
well under the 10,000/day + 1,000/hour credit limits:
  - country-name searches never reach GeoNames (resolved client-side in the UI);
  - repeat lookups are served from an on-disk cache (data/geocode_cache.sqlite by default,
    or $GEOCODE_CACHE_PATH; its own file so it never contends with the read-only malaria.db);
  - the SRTM elevation call (0.2 credit) is made only when the resolved country has an
    elevation-dependent rule, decided by the caller via locate.needs_elevation().
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import httpx

from .config import PROJECT_ROOT, geonames_username

BASE = "http://api.geonames.org"
# Overridable so a deploy can point the runtime-written cache at a writable volume (e.g. a
# Railway volume mounted at /app/var) while the baked, immutable malaria.db stays in the image.
_DEFAULT_CACHE_PATH = PROJECT_ROOT / "data" / "geocode_cache.sqlite"
CACHE_PATH = Path(os.environ.get("GEOCODE_CACHE_PATH", str(_DEFAULT_CACHE_PATH)))
_UNKNOWN_ELEV = -32768
_cache_lock = threading.Lock()
_cache_ready = False


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

    def to_dict(self) -> dict:
        return {
            "geoname_id": self.geoname_id, "name": self.name,
            "country_name": self.country_name, "country_iso2": self.country_iso2,
            "admin1": self.admin1, "admin2": self.admin2,
            "lat": self.lat, "lng": self.lng, "population": self.population,
        }


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
    )


def _get(path: str, params: dict, client: httpx.Client) -> dict:
    r = client.get(BASE + path, params=params)
    r.raise_for_status()
    return r.json()


def _rank(cands: list[GeoCandidate], q: str) -> list[GeoCandidate]:
    """Promote exact (accent-folded) name matches above population."""
    qf = _fold(q)
    def key(c: GeoCandidate) -> tuple[int, int]:
        nf = _fold(c.name)
        exact = 2 if nf == qf else (1 if nf.startswith(qf) else 0)
        return (-exact, -c.population)
    return sorted(cands, key=key)


def _search(q: str, username: str, client: httpx.Client, max_rows: int = 10) -> list[GeoCandidate]:
    base = {"q": q, "featureClass": "P", "style": "FULL", "orderby": "population",
            "maxRows": max_rows, "username": username}
    data = _get("/searchJSON", {**base, "isNameRequired": "true"}, client)
    _check_status(data)
    cands = [_candidate(g) for g in data.get("geonames", [])]
    if not cands:
        data = _get("/searchJSON", {**base, "fuzzy": "0.8"}, client)
        _check_status(data)
        cands = [_candidate(g) for g in data.get("geonames", [])]
    return _rank(cands, q)


def _by_geoname_id(geoname_id: int, username: str, client: httpx.Client) -> GeoCandidate | None:
    data = _get("/getJSON", {"geonameId": geoname_id, "style": "FULL", "username": username}, client)
    _check_status(data)
    if not data.get("geonameId"):
        return None
    return _candidate(data)


def _elevation(lat: float, lng: float, username: str, client: httpx.Client) -> int | None:
    data = _get("/srtm3JSON", {"lat": lat, "lng": lng, "username": username}, client)
    _check_status(data)
    v = data.get("srtm3")
    if v is None or v <= -32000:        # -32768 = unknown / outside SRTM coverage
        return None
    return int(v)


# --------------------------------------------------------------------------- persistent cache
def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ensure_cache() -> None:
    global _cache_ready
    if _cache_ready:
        return
    with _cache_lock:
        if _cache_ready:
            return
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(CACHE_PATH)
        conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "CREATE TABLE IF NOT EXISTS search_cache(q TEXT PRIMARY KEY, payload TEXT, fetched_at TEXT);"
            "CREATE TABLE IF NOT EXISTS geoname_cache(gid INTEGER PRIMARY KEY, payload TEXT, fetched_at TEXT);"
            "CREATE TABLE IF NOT EXISTS elev_cache(coord TEXT PRIMARY KEY, srtm3 INTEGER, fetched_at TEXT);"
        )
        conn.commit()
        conn.close()
        _cache_ready = True


def _conn() -> sqlite3.Connection:
    _ensure_cache()
    conn = sqlite3.connect(CACHE_PATH, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _cache_get_text(table: str, col: str, key) -> str | None:
    conn = _conn()
    try:
        row = conn.execute(f"SELECT payload FROM {table} WHERE {col}=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _cache_put_text(table: str, col: str, key, payload: str) -> None:
    conn = _conn()
    try:
        conn.execute(f"INSERT OR REPLACE INTO {table}({col}, payload, fetched_at) VALUES (?,?,?)",
                     (key, payload, _now()))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- public API
@lru_cache(maxsize=512)
def search_place(q: str, username: str | None = None) -> list[GeoCandidate]:
    """Ranked populated-place candidates for a query (cached; does not fetch elevation)."""
    username = _username(username)
    key = q.strip().lower()
    cached = _cache_get_text("search_cache", "q", key)
    if cached is not None:
        return [GeoCandidate(**c) for c in json.loads(cached)]
    with httpx.Client(timeout=20.0) as client:
        cands = _search(q, username, client)
    _cache_put_text("search_cache", "q", key, json.dumps([asdict(c) for c in cands]))
    return cands


@lru_cache(maxsize=512)
def lookup_geoname(geoname_id: int, username: str | None = None) -> GeoCandidate | None:
    """Resolve a specific GeoNames id (used for disambiguation); cached."""
    username = _username(username)
    cached = _cache_get_text("geoname_cache", "gid", geoname_id)
    if cached is not None:
        return GeoCandidate(**json.loads(cached))
    with httpx.Client(timeout=20.0) as client:
        cand = _by_geoname_id(geoname_id, username, client)
    if cand is not None:
        _cache_put_text("geoname_cache", "gid", geoname_id, json.dumps(asdict(cand)))
    return cand


@lru_cache(maxsize=1024)
def fetch_elevation(lat: float, lng: float, username: str | None = None) -> int | None:
    """SRTM elevation in metres (None if unknown); cached. Call only when a rule needs it."""
    username = _username(username)
    coord = f"{round(lat, 3)},{round(lng, 3)}"
    conn = _conn()
    try:
        row = conn.execute("SELECT srtm3 FROM elev_cache WHERE coord=?", (coord,)).fetchone()
    finally:
        conn.close()
    if row is not None:
        return None if row[0] == _UNKNOWN_ELEV else row[0]
    with httpx.Client(timeout=20.0) as client:
        v = _elevation(lat, lng, username, client)
    conn = _conn()
    try:
        conn.execute("INSERT OR REPLACE INTO elev_cache(coord, srtm3, fetched_at) VALUES (?,?,?)",
                     (coord, _UNKNOWN_ELEV if v is None else v, _now()))
        conn.commit()
    finally:
        conn.close()
    return v
