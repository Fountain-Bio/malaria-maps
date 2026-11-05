"""Datasette plugin: GET /robots.txt.

Datasette serves no robots.txt of its own (404), and the --static mount lands the web/
assets under /web/, so a static file can't answer the root /robots.txt that crawlers fetch.
This registers that one route.

The site is the world map at `/` and nothing else (map_only.py 404s every other path). The
policy lets crawlers fetch that page, its /web/ assets, and the map's three JSON feeds, which a
crawler that renders the map (Googlebot does) needs to draw it. Everything else is disallowed,
including /-/locate, which spends a GeoNames API credit per request. `Allow: /$` matches only
the root, and for the major crawlers (Google, Bing, DuckDuckGo, etc.) the longest matching rule
wins, so each Allow beats `Disallow: /`.

This only governs well-behaved bots. Crawlers that ignore it are throttled by
plugins/crawler_limit.py instead.
"""

from datasette import hookimpl
from datasette.utils.asgi import Response

from malaria_tracker.config import SITE_URL

ROBOTS_TXT = f"""\
# The site is the malaria world map at /. Crawlers may fetch it, its assets and the JSON it
# draws from; there is nothing else to index.
User-agent: *
Allow: /$
Allow: /web/
Allow: /malaria/country_current.json
Allow: /malaria/v_malaria_current.json
Allow: /malaria/deferral_rule.json
Disallow: /

Sitemap: {SITE_URL}/sitemap.xml
"""


@hookimpl
def register_routes():
    return [(r"^/robots\.txt$", robots_txt)]


async def robots_txt(request, datasette):
    return Response.text(ROBOTS_TXT)
