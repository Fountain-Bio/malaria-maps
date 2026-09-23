"""Datasette plugin: GET /sitemap.xml.

Gives crawlers a finite list of the pages worth indexing, so they don't have to discover
the site by following the data explorer's links (which lead into facets, sorts and
pagination). It lists the landing page, the map, the main table pages, and one page per
country: that country's current malaria_record row, dated by when that version took effect.

A new record version gets a new record_id, so a country's URL moves when CDC changes its
classification. The old row page stays up as history, and the next sitemap fetch points
crawlers at the new one.
"""

from xml.sax.saxutils import escape

from datasette import hookimpl
from datasette.utils.asgi import Response

from malaria_tracker.config import SITE_URL

STATIC_PAGES = [
    "/",
    "/web/index.html",
    "/malaria",
    "/malaria/v_malaria_current",
    "/malaria/change_event",
    "/malaria/deferral_rule",
]

CURRENT_RECORDS_SQL = """
SELECT record_id, valid_from
FROM malaria_record
WHERE is_current = 1
ORDER BY record_id
"""


@hookimpl
def register_routes():
    return [(r"^/sitemap\.xml$", sitemap_xml)]


def _url(path: str, lastmod: str | None = None) -> str:
    inner = f"<loc>{escape(SITE_URL + path)}</loc>"
    if lastmod:
        inner += f"<lastmod>{escape(lastmod)}</lastmod>"
    return f"  <url>{inner}</url>"


async def sitemap_xml(request, datasette):
    rows = await datasette.get_database("malaria").execute(CURRENT_RECORDS_SQL)
    urls = [_url(path) for path in STATIC_PAGES]
    urls += [_url(f"/malaria/malaria_record/{row['record_id']}", row["valid_from"]) for row in rows]
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + "\n".join(urls) + "\n</urlset>\n"
    )
    return Response(
        body,
        content_type="application/xml; charset=utf-8",
        headers={"cache-control": "public, max-age=3600"},
    )
