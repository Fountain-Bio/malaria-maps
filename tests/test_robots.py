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


def test_policy_allows_landing_blocks_the_rest():
    body = robots.ROBOTS_TXT
    assert "User-agent: *" in body
    assert "Allow: /$" in body              # the homepage only
    assert "Allow: /web/index.html" in body  # the map page
    assert "Disallow: /" in body            # everything else, incl. /malaria, /-/locate, world.geojson
