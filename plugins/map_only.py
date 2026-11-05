"""Datasette plugin: the site is the world map, and nothing else is served.

Datasette stays as the engine behind the map's data, but its explorer (table, row, query and
SQL pages, facets, every .json/.csv export) is switched off. Those pages were what distributed
scrapers flooded, and the map is the product. Every request is checked against PUBLIC_ROUTES
before Datasette sees it:

  - `/` serves the map page (web/index.html), with the app.js URL stamped with a hash of the
    file so a deploy that changes app.js reaches browsers that cached the previous one.
  - The map's own assets under /web/, and the three JSON feeds it fetches, each restricted to
    the query parameters web/app.js sends.
  - /-/locate (the map's city search), /-/versions.json (Railway's health check),
    /robots.txt, /sitemap.xml and /favicon.ico.

The old map URL /web/index.html (and /web/) 301s to `/`. Anything else gets a 404, and a
disallowed query parameter on a feed gets a 400. Both carry a one-hour public Cache-Control so
the CDN answers repeats of the same URL without reaching the app.
"""

import hashlib
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qsl

from datasette import hookimpl
from datasette.utils.asgi import Response

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

# Path -> the query parameter names it accepts, or None to accept any.
# The three feeds list exactly what web/app.js sends; anything else (_where, _sort, _facet,
# sql, ...) is refused, so the public surface can't be turned into arbitrary queries.
PUBLIC_ROUTES: dict[str, frozenset[str] | None] = {
    "/": frozenset(),
    "/web/app.js": frozenset({"v"}),
    "/web/world.geojson": frozenset(),
    "/malaria/country_current.json": frozenset({"_shape", "_size"}),
    "/malaria/v_malaria_current.json": frozenset({"_shape", "iso3"}),
    "/malaria/deferral_rule.json": frozenset({"_shape"}),
    "/-/locate": None,
    "/-/versions.json": frozenset(),
    "/robots.txt": frozenset(),
    "/sitemap.xml": frozenset(),
    "/favicon.ico": frozenset(),
}

OLD_MAP_PATHS = {"/web/index.html", "/web/", "/web"}
CACHEABLE_ERROR = [[b"content-type", b"text/plain; charset=utf-8"], [b"cache-control", b"public, max-age=3600"]]
APP_JS_TAG = 'src="/web/app.js"'


def check(path: str, query_string: bytes) -> tuple[int, str] | None:
    """Return (status, body) for a request that must not reach Datasette, else None."""
    if path in OLD_MAP_PATHS:
        return 301, "/"
    if path not in PUBLIC_ROUTES:
        return 404, "Not found. This site is the malaria map at /.\n"
    allowed = PUBLIC_ROUTES[path]
    if allowed is not None:
        names = {name for name, _ in parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)}
        extra = sorted(names - allowed)
        if extra:
            return 400, f"Unsupported query parameter(s) for {path}: {', '.join(extra)}\n"
    return None


@hookimpl
def asgi_wrapper(datasette):
    def wrap(app):
        @wraps(app)
        async def map_only(scope, receive, send):
            if scope.get("type") != "http":
                await app(scope, receive, send)
                return
            refusal = check(scope.get("path", ""), scope.get("query_string", b""))
            if refusal is None:
                await app(scope, receive, send)
                return
            status, body = refusal
            headers = list(CACHEABLE_ERROR)
            if status == 301:
                headers.append([b"location", body.encode()])
                body = "Moved to /\n"
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": body.encode()})

        return map_only

    return wrap


@hookimpl
def register_routes():
    return [(r"^/$", map_page)]


def render_map_page() -> str:
    """Return web/index.html with the app.js URL stamped with a hash of app.js."""
    version = hashlib.sha256((WEB_DIR / "app.js").read_bytes()).hexdigest()[:12]
    page = (WEB_DIR / "index.html").read_text()
    return page.replace(APP_JS_TAG, f'src="/web/app.js?v={version}"')


async def map_page():
    # Revalidated on every visit: this page names the current app.js URL, so it must never
    # be served stale from a cache.
    return Response.html(render_map_page(), headers={"cache-control": "no-cache"})
