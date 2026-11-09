"""审查问题 1/2 回归测试（离线）：

1. 落盘与断点续跑：正文+去重记录写盘成功后才把队列行标 done；写入失败重跑能补齐、
   不重复；store 半批写入会回滚。
2. 失败重试与状态：临时 5xx 耗尽后，后续运行能重新入队；子 sitemap 503 时整站
   不能标 complete，并保留失败入口。
"""
import json
import logging

import pytest

from crawler.channels import base
from crawler.channels import sitemap as sitemap_channel
from crawler import cursor, store
from crawler.request import RequestQueue

LOG = logging.getLogger("test.persistence")


def _rows(n, prefix="https://p.test/a"):
    return [{"url": f"{prefix}{i}", "text": "x" * 200, "title": "t"} for i in range(n)]


@pytest.fixture
def base_env(monkeypatch):
    monkeypatch.setattr(base, "normalize", lambda row, site: row)
    monkeypatch.setattr(base, "validate", lambda row, site, kind: (True, ""))
    monkeypatch.setattr(base, "extract_best",
                        lambda url, body, site, min_text=0: {"url": url, "text": "x" * 200})
    return LOG


class ListFetcher:
    """按 URL 顺序返回 200 + 固定正文。"""

    def __init__(self, body="<html><p>article</p></html>"):
        self.body = body

    def stream_requests(self, batch, workers=4):
        for req in batch:
            yield req, 200, self.body


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# ---------- 问题 1：写盘失败不得先标 done ----------
def test_rows_not_done_before_write_and_resume_fills_gap(tmp_env, base_env, monkeypatch):
    site = {"domain": "gap.test", "channel": "sitemap"}
    urls = [f"https://gap.test/{i}" for i in range(3)]
    real_append = base.append_rows
    calls = {"n": 0}

    def boom_once(domain, rows, kind="posts"):
        calls["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(base, "append_rows", boom_once)
    with pytest.raises(OSError):
        base.collect_urls(site, urls, 0, LOG, ListFetcher())

    # 写盘失败：没有正文，也没有把队列行标 done
    assert _lines(tmp_env / "gap.test" / "articles.jsonl") == []
    q = RequestQueue("gap.test")
    assert q.stats().get("unprocessed") == 3
    assert q.stats().get("done", 0) == 0
    assert cursor.load("gap.test")["status"] != "complete"

    # 重跑：补齐缺失记录，且不重复
    monkeypatch.setattr(base, "append_rows", real_append)
    r = base.collect_urls(site, urls, 0, LOG, ListFetcher())
    assert r["saved"] == 3 and r["status"] == "complete"
    assert len(_lines(tmp_env / "gap.test" / "articles.jsonl")) == 3
    assert len(_lines(tmp_env / "gap.test" / "urls.txt")) == 3

    # 再来一次：已知 URL 全部 seed done，不重复写
    r2 = base.collect_urls(site, urls, 0, LOG, ListFetcher())
    assert r2["saved"] == 0
    assert len(_lines(tmp_env / "gap.test" / "articles.jsonl")) == 3


def test_append_rows_rolls_back_when_urls_write_fails(tmp_env, monkeypatch):
    real = store._append_lines
    calls = {"n": 0}

    def fail_on_urls(path, lines):
        calls["n"] += 1
        if str(path).endswith("urls.txt"):
            raise OSError("urls write failed")
        return real(path, lines)

    monkeypatch.setattr(store, "_append_lines", fail_on_urls)
    with pytest.raises(OSError):
        store.append_rows("rb.test", _rows(2))

    assert _lines(tmp_env / "rb.test" / "articles.jsonl") == []   # 半批已回滚
    assert _lines(tmp_env / "rb.test" / "urls.txt") == []

    monkeypatch.setattr(store, "_append_lines", real)
    assert store.append_rows("rb.test", _rows(2)) == 2
    assert len(_lines(tmp_env / "rb.test" / "articles.jsonl")) == 2
    assert len(_lines(tmp_env / "rb.test" / "urls.txt")) == 2


# ---------- 问题 2：临时故障跨运行重试 ----------
class FailThenOk:
    def __init__(self):
        self.failing = True

    def stream_requests(self, batch, workers=4):
        for req in batch:
            yield req, (503 if self.failing else 200), ("busy" if self.failing else "<html><p>x</p></html>")


def test_transient_503_is_retried_on_later_run(tmp_env, base_env, monkeypatch):
    monkeypatch.setattr(base, "RETRY_COOLDOWN", 0)          # 测试里不等 15 分钟冷却
    site = {"domain": "flaky.test", "channel": "sitemap"}
    urls = ["https://flaky.test/a"]
    fetcher = FailThenOk()

    r1 = base.collect_urls(site, urls, 0, LOG, fetcher)
    assert r1["saved"] == 0
    assert r1["status"] != "complete"
    q = RequestQueue("flaky.test")
    assert q.stats().get("error") == 1                      # 单次运行内重试耗尽

    # 源站恢复后，下一次运行能重新入队并补齐
    fetcher.failing = False
    r2 = base.collect_urls(site, [], 0, LOG, fetcher)
    assert r2["saved"] == 1 and r2["status"] == "complete"


# ---------- 问题 2：子 sitemap 失败时不得标 complete ----------
class SitemapFetcher:
    def __init__(self, child_fail=False):
        self.child_fail = child_fail

    def get_many(self, urls, timeout=60):
        out = []
        for u in urls:
            if u.endswith("/sitemap.xml"):
                body = ('<sitemapindex><sitemap><loc>https://s.test/a.xml</loc></sitemap>'
                        '<sitemap><loc>https://s.test/b.xml</loc></sitemap></sitemapindex>')
                out.append((u, 200, body))
            elif u.endswith("a.xml"):
                out.append((u, 200, "<urlset><loc>https://s.test/1</loc>"
                                    "<loc>https://s.test/2</loc></urlset>"))
            elif u.endswith("b.xml"):
                if self.child_fail:
                    out.append((u, 503, ""))
                else:
                    out.append((u, 200, "<urlset><loc>https://s.test/3</loc></urlset>"))
            else:
                out.append((u, 200, "<html><p>article</p></html>"))
        return out

    def stream_requests(self, batch, workers=4):
        for req in batch:
            yield req, 200, "<html><p>article</p></html>"


def test_sitemap_child_503_keeps_partial_and_records_entry(tmp_env, base_env):
    site = {"domain": "s.test", "base": "https://s.test", "channel": "sitemap",
            "sitemap": "/sitemap.xml"}
    r = sitemap_channel.run(site, 0, LOG, SitemapFetcher(child_fail=True))
    assert r["status"] == "partial"
    cur = cursor.load("s.test")
    assert cur["status"] == "partial"
    assert cur["discovery_failed"][0]["url"] == "https://s.test/b.xml"
    assert cur["reasons"].get("discovery_http_503") == 1

    # 子图恢复后补齐，才允许 complete
    r2 = sitemap_channel.run(site, 0, LOG, SitemapFetcher(child_fail=False))
    assert r2["status"] == "complete"
    assert cursor.load("s.test")["status"] == "complete"


def test_complete_reopened_when_discovery_fails(tmp_env, base_env):
    """已 complete 的站，如果本次发现阶段有子 sitemap 503，要重新打开为 partial。"""
    site = {"domain": "done.test", "base": "https://done.test", "channel": "sitemap",
            "sitemap": "/sitemap.xml"}
    cursor.save("done.test", status="complete", done=True, saved=2,
                next_page=1, total_pages=1, reasons={})
    r = sitemap_channel.run(site, 0, LOG, SitemapFetcher(child_fail=True))
    assert r["status"] == "partial"
    cur = cursor.load("done.test")
    assert cur["status"] == "partial" and cur["done"] is False
    assert cur["last_reason"] == "discovery_failed"
