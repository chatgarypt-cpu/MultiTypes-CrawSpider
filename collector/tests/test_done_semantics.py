"""done 语义回归测试（离线，monkeypatch 伪造 HTTP / fetcher）。

对应 2026-11-07 事故：
- 空页/挑战页被当成"到底" → cookislands、wallis、fnu 假完成；
- 发现阶段 504/0 条 URL 也被标完成 → wallis。
"""
import json
import logging
import re

import pytest

from crawler import cursor
from crawler.channels import base, wp_api


# ---------- 伪造 WordPress REST ----------
def make_posts(page, n=3):
    return [{"id": page * 100 + i, "link": "https://x.test/p/%d-%d" % (page, i)}
            for i in range(n)]


class FakeWP:
    """按 URL 里的 page=N 返回伪造响应；calls 记录每页被请求次数。"""

    def __init__(self, responses, headers=None):
        self.responses = responses
        self.headers = headers or {}
        self.calls = []

    def __call__(self, session, url, timeout=90, attempts=3, impersonate=False, headers=None):
        page = int(re.search(r"[?&]page=(\d+)", url).group(1))
        self.calls.append(page)
        val = self.responses.get(page)
        if callable(val):
            val = val()
        if val is None:
            return 200, "[]", self.headers
        if isinstance(val, tuple):
            return val if len(val) == 3 else (val[0], val[1], self.headers)
        return 200, json.dumps(val), self.headers

    def count(self, page):
        return self.calls.count(page)


@pytest.fixture
def wp_site():
    return {"domain": "x.test", "base": "https://x.test", "channel": "wp-api"}


@pytest.fixture
def wp(monkeypatch):
    """patch 掉 HTTP + 映射；空页重试不真睡。"""
    monkeypatch.setattr(wp_api, "map_wp_post",
                        lambda post, site: {"url": post["link"], "text": "x" * 200,
                                            "title": "t", "created_at": ""})
    monkeypatch.setattr(wp_api, "EMPTY_RETRY_SLEEP", 0)

    def install(fake):
        monkeypatch.setattr(wp_api, "_get", fake)
        return fake
    return install


def test_walk_past_total_pages_marks_complete(tmp_env, wp_site, wp):
    fake = wp(FakeWP({1: make_posts(1), 2: make_posts(2)},
                     headers={"x-wp-totalpages": "2", "x-wp-total": "6"}))
    r = wp_api.collect_wp(wp_site, workers=8, progress=False)
    assert r["status"] == "complete"
    assert r["done"] is True
    assert r["next_page"] == 3
    cur = cursor.load("x.test")
    assert cur["status"] == "complete" and cur["done"] is True
    assert cur["total_pages"] == 2 and cur["saved"] == 6
    assert cursor.is_complete("x.test") is True
    assert fake.count(1) == 1                     # 首页探路复用了，不重复请求


def test_empty_page_jitter_is_skipped_not_complete(tmp_env, wp_site, wp):
    """第 2 页永远返回 200 []：重试后跳过继续翻，最终 partial，绝不标 complete。"""
    fake = wp(FakeWP({1: make_posts(1), 3: make_posts(3)},
                     headers={"x-wp-totalpages": "3", "x-wp-total": "6"}))
    r = wp_api.collect_wp(wp_site, workers=8, progress=False)
    assert r["status"] == "partial"
    assert r["done"] is False
    assert r["skipped_pages"] == 1
    assert fake.count(2) == 1 + wp_api.EMPTY_RETRIES   # 初次 + 重试 N 次
    cur = cursor.load("x.test")
    assert cur["status"] == "partial" and cur["done"] is False
    assert cur["reasons"]["empty_page"] == 1
    assert cursor.is_complete("x.test") is False
    assert cur["saved"] == 6                      # 第 1、3 页仍正常入库


def test_first_page_403_marks_blocked(tmp_env, wp_site, wp):
    wp(FakeWP({1: (403, "Forbidden")}))
    r = wp_api.collect_wp(wp_site, workers=8, progress=False)
    assert r["status"] == "blocked"
    assert r["done"] is False
    cur = cursor.load("x.test")
    assert cur["status"] == "blocked" and cur["done"] is False
    assert cur["blocked_reason"] == "http_403"
    assert cur["reasons"]["http_403"] == 1
    assert cursor.is_complete("x.test") is False


def test_challenge_page_non_json_is_interrupted(tmp_env, wp_site, wp):
    wp(FakeWP({1: (200, "<html><title>Just a moment...</title></html>")}))
    r = wp_api.collect_wp(wp_site, workers=8, progress=False)
    assert r["status"] == "interrupted"
    assert r["done"] is False
    cur = cursor.load("x.test")
    assert cur["status"] == "interrupted" and cur["done"] is False
    assert cur["reasons"]["non_json"] == 1
    assert cursor.is_complete("x.test") is False


def test_legacy_done_cursor_is_skipped(tmp_env, wp_site, wp):
    (tmp_env / "x.test").mkdir(parents=True, exist_ok=True)
    (tmp_env / "x.test" / "cursor.json").write_text(
        json.dumps({"next_page": 9, "done": True, "saved": 99}), encoding="utf-8")
    r = wp_api.collect_wp(wp_site, workers=8, progress=False)
    assert r.get("skipped") == "done"
    assert r["saved"] == 0


# ---------- URL-list 通道（base.collect_urls） ----------
class Fetcher:
    def __init__(self, status=200, body="<html><p>article</p></html>"):
        self.status, self.body = status, body

    def stream_requests(self, batch, workers=4):
        for req in batch:
            yield req, self.status, self.body


@pytest.fixture
def base_env(monkeypatch):
    monkeypatch.setattr(base, "normalize", lambda row, site: row)
    monkeypatch.setattr(base, "validate", lambda row, site, kind: (True, ""))
    monkeypatch.setattr(base, "extract_best",
                        lambda url, body, site, min_text=0: {"url": url, "text": "x" * 200})
    return logging.getLogger("test.base")


def test_base_discovery_empty_is_not_complete(tmp_env, base_env):
    """发现阶段 0 条 URL（504/正则不匹配）→ interrupted，不是 complete。"""
    site = {"domain": "wallis.test", "channel": "html"}
    r = base.collect_urls(site, [], 0, base_env, Fetcher())
    assert r["status"] == "interrupted"
    assert r["saved"] == 0 and r["discovered"] == 0
    cur = cursor.load("wallis.test")
    assert cur["status"] == "interrupted" and cur["done"] is False
    assert cur["reasons"]["discovery_empty"] == 1
    assert cursor.is_complete("wallis.test") is False


def test_base_drained_with_output_marks_complete(tmp_env, base_env):
    site = {"domain": "ok.test", "channel": "sitemap"}
    r = base.collect_urls(site, ["https://ok.test/a", "https://ok.test/b"], 0, base_env, Fetcher())
    assert r["status"] == "complete"
    cur = cursor.load("ok.test")
    assert cur["status"] == "complete" and cur["done"] is True
    assert cur["saved"] == 2


def test_base_all_403_marks_blocked(tmp_env, base_env):
    site = {"domain": "walled.test", "channel": "html"}
    r = base.collect_urls(site, ["https://walled.test/a"], 0, base_env,
                          Fetcher(status=403, body="forbidden"))
    assert r["status"] == "blocked"
    cur = cursor.load("walled.test")
    assert cur["status"] == "blocked" and cur["done"] is False
    assert cur["reasons"]["http_403"] >= 1


def test_base_limit_reached_is_paused(tmp_env, base_env):
    site = {"domain": "slow.test", "channel": "html"}
    urls = ["https://slow.test/%d" % i for i in range(5)]
    r = base.collect_urls(site, urls, 1, base_env, Fetcher())
    assert r["status"] == "paused"
    assert r["saved"] == 1
    cur = cursor.load("slow.test")
    assert cur["status"] == "paused" and cur["done"] is False
    assert not cursor.is_complete("slow.test")


# ---------- via: browser 的 legacy 翻页路径 ----------
class BrowserFetcher:
    """模拟 Fetcher.get_many：超过末页时返回 WP 的 400 invalid page。"""

    def __init__(self, posts_by_page, end_page):
        self.posts_by_page = posts_by_page
        self.end_page = end_page
        self.calls = []

    def get_many(self, urls, timeout=60):
        out = []
        for u in urls:
            page = int(re.search(r"[?&]page=(\d+)", u).group(1))
            self.calls.append(page)
            if page > self.end_page:
                out.append((u, 400, json.dumps({"code": "rest_post_invalid_page_number"})))
            else:
                out.append((u, 200, json.dumps(self.posts_by_page.get(page, []))))
        return out


@pytest.fixture
def browser_wp(monkeypatch):
    monkeypatch.setattr(wp_api, "map_wp_post",
                        lambda post, site: {"url": post["link"], "text": "x" * 200, "title": "t"})
    monkeypatch.setattr(wp_api, "normalize", lambda row, site: row)
    monkeypatch.setattr(wp_api, "validate", lambda row, site, kind: (True, ""))
    monkeypatch.setattr(wp_api, "EMPTY_RETRY_SLEEP", 0)
    return {"domain": "b.test", "base": "https://b.test", "channel": "wp-api", "via": "browser"}


def test_legacy_browser_400_invalid_page_marks_complete(tmp_env, browser_wp):
    log = logging.getLogger("test.legacy")
    fetcher = BrowserFetcher({1: make_posts(1), 2: make_posts(2)}, end_page=2)
    r = wp_api.run(browser_wp, 0, log, fetcher)
    assert r["status"] == "complete"
    assert r["saved"] == 6
    cur = cursor.load("b.test")
    assert cur["status"] == "complete" and cur["done"] is True


def test_legacy_browser_empty_page_marks_partial(tmp_env, browser_wp):
    log = logging.getLogger("test.legacy")
    fetcher = BrowserFetcher({1: make_posts(1), 3: make_posts(3)}, end_page=3)
    r = wp_api.run(browser_wp, 0, log, fetcher)
    assert r["status"] == "partial"
    assert r["saved"] == 6
    cur = cursor.load("b.test")
    assert cur["status"] == "partial" and cur["done"] is False
    assert cur["reasons"]["empty_page"] == 1


def test_wp_skipped_page_recovers_on_next_run(tmp_env, wp_site, wp):
    headers = {'x-wp-totalpages': '3', 'x-wp-total': '9'}
    wp(FakeWP({1: make_posts(1), 3: make_posts(3)}, headers=headers))
    assert wp_api.collect_wp(wp_site, workers=3, progress=False)['status'] == 'partial'
    fake = wp(FakeWP({2: make_posts(2), 3: make_posts(3)}, headers=headers))
    result = wp_api.collect_wp(wp_site, workers=3, progress=False)
    assert result['status'] == 'complete'
    assert result['saved'] == 3 and result['skipped_pages'] == 0
    assert 2 in fake.calls


def test_sparse_known_total_walks_past_empty_streak(tmp_env, wp_site, wp):
    site = dict(wp_site, wp_empty_page_limit=0, wp_empty_retries=0)
    fake = wp(FakeWP({1: make_posts(1), 8: make_posts(8)},
                     headers={'x-wp-totalpages': '8', 'x-wp-total': '24'}))
    result = wp_api.collect_wp(site, workers=2, progress=False)
    assert result['status'] == 'partial' and not result['done']
    assert result['saved'] == 6 and fake.count(8) == 1
    assert result['skipped_pages'] == 6


def test_unbounded_empty_scan_without_total_stops(tmp_env, wp_site, wp):
    site = dict(wp_site, wp_empty_page_limit=0, wp_empty_retries=0)
    wp(FakeWP({}))
    result = wp_api.collect_wp(site, workers=2, progress=False)
    assert result['status'] == 'interrupted' and not result['done']
