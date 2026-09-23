"""Datasette plugin: rate-limit AI training crawlers.

AI training crawlers (Meta's meta-externalagent above all) have walked the data explorer
at ~100 requests/minute, ignoring robots.txt, which made them ~95% of all traffic and
nearly all of the service's egress. They're still welcome, just slower: each crawler family
gets AI_CRAWLER_RPM requests per minute (default 30), and beyond that gets a 429 with
Retry-After, which these crawlers back off on.

Search crawlers (Googlebot, Bingbot, ...) and link-preview fetchers (facebookexternalhit)
are not matched and never limited. robots.txt and sitemap.xml are always served, so a
throttled crawler can still read the rules.

The count lives in process memory. That is enough for the single replica this service
runs (railway.toml). Only requests the CDN misses reach the app, so only those count.
"""

import os
import time
from functools import wraps

from datasette import hookimpl

# Lowercased User-Agent substrings. Each is its own budget.
AI_CRAWLERS = ("meta-externalagent", "gptbot", "claudebot", "ccbot", "bytespider", "amazonbot")
LIMIT_PER_MINUTE = int(os.environ.get("AI_CRAWLER_RPM", "30"))
WINDOW_SECONDS = 60
EXEMPT_PATHS = {"/robots.txt", "/sitemap.xml"}

# crawler family -> (window start, requests seen in that window)
_windows: dict[str, tuple[float, int]] = {}


def crawler_family(user_agent: str) -> str | None:
    ua = user_agent.lower()
    return next((name for name in AI_CRAWLERS if name in ua), None)


def retry_after(family: str, now: float) -> int | None:
    """Count one request for this crawler. Return seconds to wait if over budget, else None."""
    start, count = _windows.get(family, (now, 0))
    if now - start >= WINDOW_SECONDS:
        start, count = now, 0
    if count >= LIMIT_PER_MINUTE:
        return max(1, int(start + WINDOW_SECONDS - now))
    _windows[family] = (start, count + 1)
    return None


def _user_agent(scope) -> str:
    for name, value in scope.get("headers") or []:
        if name.lower() == b"user-agent":
            return value.decode("latin-1")
    return ""


@hookimpl
def asgi_wrapper(datasette):
    def wrap(app):
        @wraps(app)
        async def limited(scope, receive, send):
            if scope.get("type") != "http" or scope.get("path") in EXEMPT_PATHS:
                await app(scope, receive, send)
                return
            family = crawler_family(_user_agent(scope))
            wait = retry_after(family, time.monotonic()) if family else None
            if wait is None:
                await app(scope, receive, send)
                return
            # no-store: the CDN must never serve this 429 to anyone else.
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        [b"content-type", b"text/plain; charset=utf-8"],
                        [b"retry-after", str(wait).encode()],
                        [b"cache-control", b"no-store"],
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"Too many requests, slow down.\n"})

        return limited

    return wrap
