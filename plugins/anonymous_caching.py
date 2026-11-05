"""Datasette plugin: serve every request as anonymous so the CDN can cache the API.

Datasette treats any request that carries a Cookie or Authorization header as personalized:
it answers with `Cache-Control: private, no-store`, and it adds `Vary: Cookie, Authorization`
to every dynamic response. Railway's CDN never stores a response marked private or one that
varies on Cookie, so left alone those two headers turn the hour-long default_cache_ttl into
no edge caching at all, and every request lands on the single replica.

Nothing on this site is personalized. There are no logins, actors or permission rules, and the
database is opened immutable. The cookies that do arrive belong to other things: the Railway
edge's browser-check clearance, and analytics or parent-domain cookies. So this wrapper drops
the Cookie and Authorization request headers before Datasette sees them, and removes Cookie
and Authorization from the response's Vary header, leaving Accept-Encoding and any other
values in place.

The /web/ static mount is left alone: Datasette already exempts it, and cache_headers.py
stamps its headers.
"""

from functools import wraps

from datasette import hookimpl

STATIC_PREFIX = "/web/"
CREDENTIAL_HEADERS = {b"cookie", b"authorization"}


def strip_credentials(headers) -> list:
    """Return the request headers minus Cookie and Authorization."""
    return [pair for pair in headers if pair[0].lower() not in CREDENTIAL_HEADERS]


def drop_credential_vary(headers) -> list:
    """Return the response headers with Cookie and Authorization removed from Vary.

    A Vary header left empty by the removal is dropped entirely.
    """
    out = []
    for name, value in headers:
        if name.lower() != b"vary":
            out.append([name, value])
            continue
        kept = [part.strip() for part in value.split(b",") if part.strip().lower() not in CREDENTIAL_HEADERS]
        kept = [part for part in kept if part]
        if kept:
            out.append([name, b", ".join(kept)])
    return out


@hookimpl
def asgi_wrapper(datasette):
    def wrap(app):
        @wraps(app)
        async def anonymous(scope, receive, send):
            if scope.get("type") != "http" or scope.get("path", "").startswith(STATIC_PREFIX):
                await app(scope, receive, send)
                return

            async def wrapped_send(event):
                if event["type"] == "http.response.start":
                    event = {**event, "headers": drop_credential_vary(event.get("headers") or [])}
                await send(event)

            scope = {**scope, "headers": strip_credentials(scope.get("headers") or [])}
            await app(scope, receive, wrapped_send)

        return anonymous

    return wrap
