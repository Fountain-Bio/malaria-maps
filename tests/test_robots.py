"""Tests for the /robots.txt route plugin (no Datasette boot, no network)."""

import asyncio
import importlib.util
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "robots.py"


def _load():
    spec = importlib.util.spec_from_file_location("robots_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


robots = _load()


def test_route_is_root_robots_txt():
    routes = robots.register_routes()
    assert len(routes) == 1
    pattern, view = routes[0]
    assert pattern == r"^/robots\.txt$"
    assert view is robots.robots_txt


def test_handler_serves_plain_text_200():
    resp = asyncio.run(robots.robots_txt(request=None, datasette=None))
    assert resp.status == 200
    assert resp.content_type.startswith("text/plain")
    assert resp.body == robots.ROBOTS_TXT


def test_policy_blocks_the_crawl_trap_not_the_pages():
    body = robots.ROBOTS_TXT
    assert "User-agent: *" in body
    assert "Disallow: /\n" not in body          # pages themselves are crawlable
    for rule in ("Disallow: /*?", "Disallow: /*.json", "Disallow: /*.csv", "Disallow: /-/"):
        assert rule in body                     # facets/sorts/pagination/SQL, exports, /-/locate
    assert "Allow: /malaria/country_current.json" in body  # the map's data, for rendering crawlers
    assert "Sitemap: https://" in body and body.rstrip().endswith("/sitemap.xml")
