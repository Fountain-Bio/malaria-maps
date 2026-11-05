"""Datasette plugin: GET /sitemap.xml.

The site is a single page, the world map at `/` (see map_only.py), so the sitemap lists that
one URL. Search engines that render JavaScript reach the per-country detail through the map.
"""

from xml.sax.saxutils import escape

from datasette import hookimpl
from datasette.utils.asgi import Response

from malaria_tracker.config import SITE_URL

SITEMAP_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    f"  <url><loc>{escape(SITE_URL + '/')}</loc></url>\n"
    "</urlset>\n"
)


@hookimpl
def register_routes():
    return [(r"^/sitemap\.xml$", sitemap_xml)]


async def sitemap_xml(request, datasette):
    return Response(
        SITEMAP_XML,
        content_type="application/xml; charset=utf-8",
        headers={"cache-control": "public, max-age=3600"},
    )
