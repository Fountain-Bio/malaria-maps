"""Datasette plugin: cache-control headers for the static web/ mount.

Datasette's --static serving emits no Cache-Control at all (datasette#1645), which makes the
heaviest asset we ship (web/world.geojson) effectively uncacheable behind a CDN. This stamps
headers on /web/* responses only:

  - far-future immutable for the content-stable assets (world.geojson, app.js). The baked
    image is the unit of versioning: a rebuild that changes these files is a new deploy, so
    a year-long immutable cache never needs purging.
  - no-cache for index.html (and any other html), the single entry point that names the
    current asset URLs and must always be revalidated.

Everything outside /web/ passes through untouched, so this never clobbers the
max-age=31536000 headers datasette-hashed-urls sets on the /malaria-<hash> API.
"""

from functools import wraps

from datasette import hookimpl

STATIC_PREFIX = "/web/"
IMMUTABLE = b"public, max-age=31536000, immutable"
NO_CACHE = b"no-cache"


def _cache_value(path: str) -> bytes | None:
    """Return the Cache-Control value for a static path, or None to leave the response alone."""
    if not path.startswith(STATIC_PREFIX):
        return None
    if path.endswith((".geojson", ".js")):
        return IMMUTABLE
    # index.html, the bare /web/, and anything else under the mount: revalidate every time.
    return NO_CACHE


@hookimpl
def asgi_wrapper(datasette):
    def wrap(app):
        @wraps(app)
        async def stamped(scope, receive, send):
            if scope.get("type") != "http":
                await app(scope, receive, send)
                return
            cache_value = _cache_value(scope.get("path", ""))
            if cache_value is None:
                await app(scope, receive, send)
                return

            async def wrapped_send(event):
                # Only stamp successful responses. A far-future header on a 404 would pin the
                # error in every cache between here and the browser.
                if event["type"] == "http.response.start" and event.get("status") == 200:
                    headers = [
                        pair for pair in (event.get("headers") or []) if pair[0].lower() != b"cache-control"
                    ]
                    headers.append([b"cache-control", cache_value])
                    event = {**event, "headers": headers}
                await send(event)

            await app(scope, receive, wrapped_send)

        return stamped

    return wrap
