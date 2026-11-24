"""CDX → 队列：过滤资源/查询串，只入队未采过的 URL。离线，monkeypatch requests.get。"""
import pytest

import cdx_seed
from crawler.request import RequestQueue


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_cdx_filters_assets_queries_and_junk(monkeypatch):
    payload = [
        ["original", "timestamp"],
        ["http://example.org/-community-news.html", "20240101"],
        ["http://example.org/-sports-report.html", "20240102"],
        ["http://example.org/-sports-report.html", "20240103"],   # 去重
        ["http://example.org/images/logo.png", "20240104"],       # 资源
        ["http://example.org/page.html?id=3", "20240105"],        # 查询串
        ["http://example.org/wp-admin/", "20240106"],             # 后台
        ["http://example.org/%E4%B8%AD.html", "20240107"],        # 编码垃圾
    ]
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(params or {})
        return FakeResp(payload)

    monkeypatch.setattr(cdx_seed.requests, "get", fake_get)
    urls = cdx_seed.cdx_urls("example.org", limit=100)
    assert urls == ["http://example.org/-community-news.html",
                    "http://example.org/-sports-report.html"]
    assert seen["collapse"] == "urlkey" and "mimetype:text/html" in seen["filter"]


def test_seed_queues_only_unknown_urls(tmp_env, monkeypatch):
    domain = "example.org"
    payload = [["original", "timestamp"],
               ["http://example.org/a.html", "20240101"],
               ["http://example.org/b.html", "20240102"]]
    monkeypatch.setattr(cdx_seed.requests, "get",
                        lambda *a, **k: FakeResp(payload))
    urls = cdx_seed.cdx_urls(domain)
    res = cdx_seed.seed(domain, urls)
    assert res["queued"] == 2 and res["added"] == 2
    q = RequestQueue(domain)
    assert q.stats() == {"unprocessed": 2}
    first = q.next_batch(2)[0]
    assert first.channel == "cdx" and first.label == "detail"


def test_seed_dry_run_writes_nothing(tmp_env, monkeypatch):
    domain = "dry.cdx.test"
    monkeypatch.setattr(cdx_seed.requests, "get",
                        lambda *a, **k: FakeResp([["original", "timestamp"],
                                                  ["http://dry.cdx.test/x.html", "20240101"]]))
    res = cdx_seed.seed(domain, cdx_seed.cdx_urls(domain), dry_run=True)
    assert res["queued"] == 1 and res["added"] == 0
    assert RequestQueue(domain).stats() == {}
