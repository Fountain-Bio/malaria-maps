"""Datasette plugin: GET /robots.txt.

Datasette serves no robots.txt of its own (404), and the --static mount lands the web/
assets under /web/, so a static file can't answer the root /robots.txt that crawlers fetch.
This registers that one route.

The policy lets crawlers index the site's finite pages: the landing page, the map, the
table pages, and per-row pages (sitemap.xml lists the ones worth indexing). It keeps them
out of the combinatorial part of the data explorer, where every facet, sort, filter,
pagination cursor, ad-hoc SQL query and .json/.csv export is its own URL, and out of
/-/locate, which spends a GeoNames API credit per request.

The map's own data fetches are .json URLs with query strings, so they are allowed back in
explicitly: a crawler that renders the map (Googlebot does) needs them to draw it. The
longest matching rule wins for the major crawlers (Google, Bing, DuckDuckGo, etc.), so each
Allow below beats the shorter Disallow patterns.

This only governs well-behaved bots. Crawlers that ignore it are throttled by
plugins/crawler_limit.py instead.
"""

from datasette import hookimpl
from datasette.utils.asgi import Response

from malaria_tracker.config import SITE_URL

ROBOTS_TXT = f"""\
# Index the landing page, the map, the table pages and per-row pages. Keep crawlers out
# of the data explorer's endless URL variants (facets, sorts, filters, pagination, SQL,
# .json/.csv exports) and the /-/locate geocoder.
User-agent: *
Disallow: /*?
Disallow: /*.json
Disallow: /*.csv
Disallow: /-/
Allow: /-/static/
Allow: /malaria/country_current.json
Allow: /malaria/v_malaria_current.json
Allow: /malaria/deferral_rule.json

Sitemap: {SITE_URL}/sitemap.xml
"""


@hookimpl
def register_routes():
    return [(r"^/robots\.txt$", robots_txt)]


async def robots_txt(request, datasette):
    return Response.text(ROBOTS_TXT)
