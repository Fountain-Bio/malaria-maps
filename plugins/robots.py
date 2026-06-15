"""Datasette plugin: GET /robots.txt.

Datasette serves no robots.txt of its own (404), and the --static mount lands the web/
assets under /web/, so a static file can't answer the root /robots.txt that crawlers fetch.
This registers that one route.

The policy lets compliant crawlers index the landing page (the world map) and the script it
loads, and closes off everything else. The reason is bandwidth: the Datasette data explorer
under /malaria is a combinatorial crawl space (every facet combination, per-row page, and
.json/.csv export is its own URL), /-/locate spends a GeoNames API credit per request, and
/web/world.geojson is an ~800 KB polygon file. A crawler walking all of that burns bandwidth
(and GeoNames credits) for no indexing value.

This only governs well-behaved bots; abusive scrapers ignore robots.txt, for which edge
rate-limiting is the real backstop. Allow/$ and the longest-match precedence used here are
honored by the major crawlers (Google, Bing, DuckDuckGo, etc.).
"""

from datasette import hookimpl
from datasette.utils.asgi import Response

ROBOTS_TXT = """\
# Index the landing page (the world map) and the script it needs to render.
# Everything else - the /malaria data explorer, its faceted/exported URLs, the
# /-/locate geocoder, and the ~800 KB world.geojson - is off-limits to crawlers.
User-agent: *
Allow: /$
Allow: /web/index.html
Allow: /web/app.js
Disallow: /
"""


@hookimpl
def register_routes():
    return [(r"^/robots\.txt$", robots_txt)]


async def robots_txt(request, datasette):
    return Response.text(ROBOTS_TXT)
