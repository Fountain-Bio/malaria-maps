"""Crawler-facing behavior, against a booted Datasette over the committed DB (no network)."""

import asyncio
import importlib.util
import re
from pathlib import Path

from datasette.app import Datasette

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "plugins" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


crawler_limit = _load("crawler_limit")


def _get_all(paths, headers=None):
    # Mirrors the Dockerfile's serve command: immutable DB, the web/ static mount, the plugins dir.
    ds = Datasette(
        immutables=[str(ROOT / "data" / "malaria.db")],
        plugins_dir=str(ROOT / "plugins"),
        static_mounts=[("web", str(ROOT / "web"))],
    )

    async def go():
        await ds.invoke_startup()
        return [await ds.client.get(p, headers=headers or {}, follow_redirects=False) for p in paths]

    return asyncio.run(go())


def _get(path, headers=None):
    return _get_all([path], headers)[0]


def test_sitemap_lists_only_the_map_page():
    resp = _get("/sitemap.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    assert resp.text.count("<loc>") == 1
    assert "<loc>https://malariatracker.com/</loc>" in resp.text


def test_root_serves_the_map_with_a_versioned_app_js():
    resp = _get("/")
    assert resp.status_code == 200
    assert "<h1>Malaria Region Tracker</h1>" in resp.text
    assert re.search(r'src="/web/app\.js\?v=[0-9a-f]{12}"', resp.text)
    assert resp.headers["cache-control"] == "no-cache"
    version = re.search(r"app\.js\?v=([0-9a-f]{12})", resp.text).group(1)
    assert _get(f"/web/app.js?v={version}").status_code == 200


def test_old_map_url_redirects_to_root():
    for path in ("/web/index.html", "/web/"):
        resp = _get(path)
        assert resp.status_code == 301, path
        assert resp.headers["location"] == "/"


def test_map_feeds_serve_the_parameters_the_map_sends():
    paths = [
        "/malaria/country_current.json?_shape=array&_size=max",
        "/malaria/v_malaria_current.json?_shape=array&iso3=AFG",
        "/malaria/deferral_rule.json?_shape=array",
        "/web/world.geojson",
        "/-/versions.json",
        "/robots.txt",
    ]
    for path, resp in zip(paths, _get_all(paths), strict=True):
        assert resp.status_code == 200, path
    afg = _get("/malaria/v_malaria_current.json?_shape=array&iso3=AFG").json()
    assert len(afg) == 1 and afg[0]["iso3"] == "AFG"


def test_explorer_and_exports_are_gone():
    paths = [
        "/malaria",
        "/malaria/malaria_record",
        "/malaria/malaria_record.csv",
        "/malaria/malaria_record.json",
        "/malaria/v_malaria_current",
        "/malaria/v_malaria_current.csv",
        "/malaria/malaria_record/1",
        "/malaria/country_history?country=Afghanistan",
        "/malaria?sql=select+1",
        "/malaria.db",
        "/malaria-35afc4b/v_malaria_current",
        "/-/metadata",
    ]
    for path, resp in zip(paths, _get_all(paths), strict=True):
        assert resp.status_code == 404, path
        assert "max-age=3600" in resp.headers["cache-control"], path


def test_map_feeds_refuse_other_query_parameters():
    for path in (
        "/malaria/country_current.json?_shape=array&_where=1%3D1",
        "/malaria/v_malaria_current.json?_shape=array&_facet=screening_class",
        "/malaria/deferral_rule.json?_shape=array&_sort=code",
    ):
        assert _get(path).status_code == 400, path


def test_requests_with_cookies_stay_cacheable_at_the_edge():
    # Datasette marks any request with a cookie as private and varies every response on Cookie;
    # Railway's CDN stores neither. The edge's browser-check clearance cookie and analytics
    # cookies arrive on ordinary visits, so the API must stay public and shareable anyway.
    headers = {"cookie": "cf_clearance=abc; _ga=GA1.1.1", "accept-encoding": "gzip"}
    for path in ("/malaria/country_current.json?_shape=array", "/malaria/v_malaria_current.json?iso3=AFG"):
        resp = _get(path, headers=headers)
        assert resp.status_code == 200, path
        cache_control = resp.headers["cache-control"]
        assert "max-age=" in cache_control and "private" not in cache_control, (path, cache_control)
        vary = [part.strip().lower() for part in resp.headers["vary"].split(",")]
        assert "cookie" not in vary and "authorization" not in vary, (path, vary)
        assert "accept-encoding" in vary, (path, vary)


def test_crawler_family_matches_ai_crawlers_only():
    meta = "Mozilla/5.0 (compatible; meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler))"
    assert crawler_limit.crawler_family(meta) == "meta-externalagent"
    assert crawler_limit.crawler_family("Mozilla/5.0 (compatible; GPTBot/1.2)") == "gptbot"
    assert crawler_limit.crawler_family("Mozilla/5.0 (compatible; Googlebot/2.1)") is None
    assert crawler_limit.crawler_family("facebookexternalhit/1.1") is None


def test_budget_resets_each_window():
    crawler_limit._windows.clear()
    limit = crawler_limit.LIMIT_PER_MINUTE
    assert all(crawler_limit.retry_after("gptbot", 1000.0) is None for _ in range(limit))
    assert crawler_limit.retry_after("gptbot", 1010.0) == 50
    assert crawler_limit.retry_after("ccbot", 1010.0) is None          # separate budget
    assert crawler_limit.retry_after("gptbot", 1060.0) is None         # new window


def test_over_budget_crawler_gets_uncacheable_429():
    # Datasette loads its own copy of the plugin, so go over budget with real requests.
    ua = {"user-agent": "Mozilla/5.0 (compatible; Bytespider)"}
    paths = ["/-/versions.json"] * (crawler_limit.LIMIT_PER_MINUTE + 1) + ["/robots.txt"]
    *allowed, limited, robots_txt = _get_all(paths, headers=ua)
    assert all(r.status_code == 200 for r in allowed)
    assert limited.status_code == 429
    assert limited.headers["cache-control"] == "no-store"   # the CDN must not serve it to others
    assert int(limited.headers["retry-after"]) >= 1
    assert robots_txt.status_code == 200                    # rules stay readable
