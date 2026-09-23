"""Datasette plugin: 301 the retired /malaria-<hash>/... URLs to /malaria/...

The API used to be served under a per-build content hash (datasette-hashed-urls). Every
weekly rebake changed the hash, so crawlers kept revisiting old /malaria-<hash> links and
getting 302s: over half of all requests were those redirects. The database is now served
at the stable /malaria path, and any old hashed link, whatever its hash, gets a permanent
redirect to the same path and query string there. A 301 tells crawlers to update their
link, and the CDN can cache it, so repeat hits never reach the app.
"""

from datasette import hookimpl
from datasette.utils.asgi import Response

LEGACY_PATH = r"^/malaria-[0-9a-f]{7}(?P<rest>[/.].*)?$"


@hookimpl
def register_routes():
    return [(LEGACY_PATH, legacy_redirect)]


async def legacy_redirect(request, datasette):
    target = "/malaria" + (request.url_vars.get("rest") or "")
    if request.query_string:
        target += "?" + request.query_string
    return Response.redirect(target, status=301, headers={"cache-control": "public, max-age=86400"})
