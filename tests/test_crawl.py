"""Crawler-facing behavior, against a booted Datasette over the committed DB (no network)."""

import asyncio
import importlib.util
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
    ds = Datasette(immutables=[str(ROOT / "data" / "malaria.db")], plugins_dir=str(ROOT / "plugins"))

    async def go():
        await ds.invoke_startup()
        return [await ds.client.get(p, headers=headers or {}, follow_redirects=False) for p in paths]

    return asyncio.run(go())


def _get(path, headers=None):
    return _get_all([path], headers)[0]


def test_sitemap_lists_pages_and_one_row_per_country():
    resp = _get("/sitemap.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    body = resp.text
    assert "<loc>https://malariatracker.com/web/index.html</loc>" in body
    assert body.count("/malaria/malaria_record/") >= 200   # one current record per country
    assert "<lastmod>" in body


def test_legacy_hashed_urls_301_to_stable_path():
    resp = _get("/malaria-35afc4b/malaria_record?_next=100")
    assert resp.status_code == 301
    assert resp.headers["location"] == "/malaria/malaria_record?_next=100"
    assert "max-age" in resp.headers["cache-control"]
    assert _get("/malaria-35afc4b").headers["location"] == "/malaria"


def test_stable_path_serves_directly_gzipped():
    resp = _get("/malaria/malaria_record", headers={"accept-encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in resp.headers["vary"].lower()


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
