"""逆向采集：CDX 发现 → 存档取文（channel: archive / via: archive）。全部离线。"""
import logging

import pytest

import cdx_seed
from crawler import web
from crawler.channels import archive, base

LOG = logging.getLogger(__name__)
GOOD = ("<html><head><title>Story</title></head><body><article>"
        + ("archive body text " * 100) + "</article></body></html>")


def test_discover_uses_cdx_and_skips_when_queue_has_work(tmp_env, monkeypatch):
    calls = []
    monkeypatch.setattr(cdx_seed, "cdx_urls",
                        lambda domain, limit=5000, since=None, pages_only=True: calls.append(domain) or
                        ["http://x.test/a.html", "http://x.test/b.html"])
    site = {"domain": "x.test", "base": "https://x.test", "channel": "archive"}
    urls, failures = archive.discover(site)
    assert urls == ["http://x.test/a.html", "http://x.test/b.html"] and failures == []

    from crawler.request import RequestQueue
    RequestQueue("x.test").add_many(urls[:1], channel="archive")
    urls2, _ = archive.discover(site)
    assert urls2 == [] and len(calls) == 1          # 队列已有待办，不再打 CDX


def test_discover_match_filters_urls(tmp_env, monkeypatch):
    monkeypatch.setattr(cdx_seed, "cdx_urls",
                        lambda *a, **k: ["http://x.test/2020/a.html", "http://x.test/2019/b.html"])
    site = {"domain": "x.test", "base": "https://x.test", "cdx": {"match": "/2020/"}}
    urls, failures = archive.discover(site)
    assert urls == ["http://x.test/2020/a.html"] and failures == []


def test_discover_reports_cdx_failure_as_discovery_failure(tmp_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("CDX 查询失败：timeout")
    monkeypatch.setattr(cdx_seed, "cdx_urls", boom)
    urls, failures = archive.discover({"domain": "x.test", "base": "https://x.test"})
    assert urls == [] and failures and failures[0]["status"] == 0
    assert "CDX 查询失败" in failures[0]["error"]


def test_run_saves_rows_from_archive_bodies(tmp_env, monkeypatch):
    monkeypatch.setattr(cdx_seed, "cdx_urls", lambda *a, **k: ["https://x.test/a.html"])
    monkeypatch.setattr(base, "extract_best",
                        lambda url, body, site, min_text=0: {
                            "url": url, "title": "A", "text": "archive body text " * 40})

    class Fetcher:
        def stream_requests(self, batch, workers=4):
            yield from ((req, 200, GOOD) for req in batch)

    site = {"domain": "x.test", "channel": "archive", "base": "https://x.test", "via": "archive"}
    res = archive.run(site, 0, LOG, Fetcher())
    assert res["saved"] == 1 and res["status"] == "complete"


def test_fetcher_archive_via_calls_paywall(monkeypatch, tmp_env):
    from crawler import paywall
    monkeypatch.setattr(paywall, "archive_fetch",
                        lambda url: (200, GOOD, "archive_org"))
    f = web.Fetcher({"domain": "x.test", "base": "https://x.test", "via": "archive"})
    url, status, body = f._fetch_one("https://x.test/a.html")
    assert (status, body) == (200, GOOD)


def test_fetcher_archive_via_reports_miss(monkeypatch, tmp_env):
    from crawler import paywall
    monkeypatch.setattr(paywall, "archive_fetch", lambda url: (0, "", ""))
    f = web.Fetcher({"domain": "x.test", "base": "https://x.test", "via": "archive"})
    _, status, body = f._fetch_one("https://x.test/a.html")
    assert status == 0 and "archive_miss" in body


def test_fetcher_proxy_mode_uses_pool(monkeypatch, tmp_env):
    from crawler import proxies
    seen = {}

    def fake_get_text(url, headers, timeout=None, verify=True, documents=False,
                      public_host=None, proxies=None):
        seen["proxies"] = proxies
        return 200, GOOD

    monkeypatch.setattr(web, "get_text", fake_get_text)
    site = {"domain": "p.test", "base": "https://p.test",
            "via": "proxy", "proxy": "http://127.0.0.1:8080", "robots": False}
    f = web.Fetcher(site)
    _, status, _ = f._fetch_one("https://p.test/a")
    assert status == 200
    assert seen["proxies"] == {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}
