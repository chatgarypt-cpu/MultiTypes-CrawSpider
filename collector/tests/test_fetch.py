"""抓取层测试：连接池 / 体积与时延上限 / 重试 / robots 分组与 Crawl-delay / 持久化。

全部离线：要么 monkeypatch `web.SESSION.get` 返回假响应，要么访问 127.0.0.1 的本地
http.server。不访问外网、不写 data/。
"""
import json
import time as _time

import pytest

from crawler import politeness, web


class FakeResponse:
    def __init__(self, status=200, body=b"", ctype="text/html; charset=utf-8", headers=None, chunks=None):
        self.status_code = status
        self.encoding = "utf-8"
        self.closed = False
        self.headers = {"content-type": ctype}
        if headers:
            self.headers.update({k.lower(): v for k, v in headers.items()})
        self._chunks = chunks if chunks is not None else ([body] if body else [])

    def iter_content(self, size=65536):
        yield from self._chunks

    def close(self):
        self.closed = True


@pytest.fixture
def fake_get(monkeypatch):
    """把 web.SESSION.get 换掉，记录调用并按队列返回假响应/异常/工厂。"""
    calls = []
    queue = []

    def _get(url, **kwargs):
        calls.append((url, kwargs))
        item = queue.pop(0) if queue else FakeResponse(body=b"ok")
        if callable(item) and not isinstance(item, FakeResponse):
            return item(url, **kwargs)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(web.SESSION, "get", _get)

    class Helper:
        def __init__(self, calls, queue):
            self.calls = calls
            self.queue = queue

        def push(self, *items):
            self.queue.extend(items)
            return self

    return Helper(calls, queue)


# ---------- 连接池 ----------
def test_shared_session_pool_config():
    assert isinstance(web.SESSION, web.requests.Session)
    for scheme in ("http://", "https://"):
        adapter = web.SESSION.adapters[scheme]
        assert isinstance(adapter, web.requests.adapters.HTTPAdapter)
        assert adapter._pool_connections == web.POOL_CONNECTIONS == 16
        assert adapter._pool_maxsize == web.POOL_MAXSIZE == 32
    # 默认超时：connect=10 / read=25
    assert web._as_timeout((10, 25)) == (10, 25)
    assert web._as_timeout(90) == (10, 25)


def test_get_text_reuses_module_session(fake_get):
    fake_get.push(FakeResponse(body=b"one"), FakeResponse(body=b"two"))
    assert web.get_text("http://a.test/1", {}) == (200, "one")
    assert web.get_text("http://a.test/2", {}) == (200, "two")
    # 两次请求都打到同一个 SESSION（没有新建连接池）
    assert [u for u, _ in fake_get.calls] == ["http://a.test/1", "http://a.test/2"]


# ---------- 体积 / 时延 ----------
def test_body_cap_rejects_incomplete_html_without_retry(fake_get, monkeypatch):
    monkeypatch.setattr(web, "MAX_BODY_BYTES", 1000)
    fake_get.push(FakeResponse(chunks=[b"x" * 700] * 5))
    with pytest.raises(web.ResponseSizeLimit):
        web.get_text("http://a.test/big", {})
    assert len(fake_get.calls) == 1


def test_body_deadline_stops_slow_drip(monkeypatch):
    clock = iter([0.0, 0.5, 100.0])
    monkeypatch.setattr(web.time, "monotonic", lambda: next(clock))
    resp = FakeResponse(chunks=[b"a" * 10, b"b" * 10, b"c" * 10])
    body = web.read_body_capped(resp, max_bytes=10 ** 6, deadline=10)
    assert body == "a" * 10 + "b" * 10      # 第 3 块前已超过总时延


def test_non_textual_content_type_skipped(fake_get):
    pdf = FakeResponse(ctype="application/pdf", chunks=[b"%PDF-1.4"])
    fake_get.push(pdf)
    status, body = web.get_text("http://a.test/file.pdf", {})
    assert status == 200 and body == ""
    assert pdf.closed


# ---------- 重试 ----------
def test_retry_on_5xx_with_backoff(fake_get, monkeypatch):
    sleeps = []
    monkeypatch.setattr(web.time, "sleep", lambda s: sleeps.append(s))
    fake_get.push(FakeResponse(status=502), FakeResponse(status=200, body=b"ok"))
    assert web.get_text("http://a.test/x", {}) == (200, "ok")
    assert len(fake_get.calls) == 2
    assert sleeps == [web.RETRY_BACKOFF]


def test_retry_on_connection_error_exponential(fake_get, monkeypatch):
    sleeps = []
    monkeypatch.setattr(web.time, "sleep", lambda s: sleeps.append(s))
    fake_get.push(RuntimeError("boom"), RuntimeError("boom"), FakeResponse(body=b"ok"))
    assert web.get_text("http://a.test/x", {}) == (200, "ok")
    assert len(fake_get.calls) == 3                # 2 次重试后成功
    assert sleeps == [web.RETRY_BACKOFF, web.RETRY_BACKOFF * 2]


def test_gives_up_after_retries(fake_get, monkeypatch):
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    fake_get.push(RuntimeError("a"), RuntimeError("b"), RuntimeError("c"))
    status, body = web.get_text("http://a.test/x", {})
    assert status == 0 and "c" in body
    assert len(fake_get.calls) == 3


# ---------- Fetcher / _get 统一到连接池 ----------
def test_get_many_preserves_order_and_uses_session(fake_get):
    fake_get.push(lambda url, **kw: FakeResponse(body=url.encode()),
                  lambda url, **kw: FakeResponse(body=url.encode()))
    fetcher = web.Fetcher({"domain": "order.test", "robots": False})
    out = fetcher.get_many(["http://a.test/1", "http://a.test/2"])
    assert out == [("http://a.test/1", 200, "http://a.test/1"),
                   ("http://a.test/2", 200, "http://a.test/2")]
    assert [u for u, _ in fake_get.calls] == ["http://a.test/1", "http://a.test/2"]


def test_get_goes_through_pooled_session(fake_get):
    fake_get.push(FakeResponse(body=b'{"ok": 1}'))
    status, body, headers = web._get(None, "http://pool.test/api")
    assert status == 200 and json.loads(body) == {"ok": 1}
    url, kwargs = fake_get.calls[0]
    assert url == "http://pool.test/api"
    assert kwargs["timeout"] == (10, 25)      # connect=10 / read<=25
    assert kwargs["stream"] is True
    assert headers["content-type"].startswith("text/html")


def test_stream_many_passes_crawl_delay(monkeypatch):
    acquired = []

    class LockedLimiter:
        def acquire(self, url, timeout=30, min_interval=None):
            acquired.append(min_interval)

        def penalize(self, url):
            pass

        def reward(self, url):
            pass

    class Robots:
        def allowed(self, url):
            return True

        def crawl_delay(self, url):
            return 3.0

    monkeypatch.setattr(web, "LIMITER", LockedLimiter())
    monkeypatch.setattr(web, "ROBOTS", Robots())
    monkeypatch.setattr(web, "get_text", lambda url, headers, timeout: (200, "B" + url[-1]))
    out = sorted(web.Fetcher({"domain": "stream.test"}).stream_many(["http://a.test/1", "http://a.test/2"]))
    assert out == [("http://a.test/1", 200, "B1"), ("http://a.test/2", 200, "B2")]
    assert acquired == [3.0, 3.0]


# ---------- robots ----------
class FakeRobotsSession:
    def __init__(self, status=200, text=""):
        self.status = status
        self.text = text
        self.calls = []

    def get(self, url, headers=None, timeout=None, stream=False):
        self.calls.append(url)
        return FakeResponse(status=self.status, body=self.text.encode())


def test_robots_ignores_non_star_groups():
    """GPTBot 的 Disallow: / 不能把全站禁掉（今天的事故 5）。"""
    text = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    cache = politeness.RobotsCache(session=FakeRobotsSession(200, text), persist=False)
    assert cache.allowed("http://a.test/news/1") is True


def test_robots_star_disallow_blocks():
    text = "User-agent: *\nDisallow: /private\n"
    cache = politeness.RobotsCache(session=FakeRobotsSession(200, text), persist=False)
    assert cache.allowed("http://a.test/private/x") is False
    assert cache.allowed("http://a.test/news") is True


def test_robots_allow_beats_longer_disallow():
    text = "User-agent: *\nDisallow: /private\nAllow: /private/ok\n"
    cache = politeness.RobotsCache(session=FakeRobotsSession(200, text), persist=False)
    assert cache.allowed("http://a.test/private/ok/1") is True
    assert cache.allowed("http://a.test/private/x") is False


def test_robots_network_error_allows():
    class Boom:
        def get(self, *a, **k):
            raise OSError("no network")

    cache = politeness.RobotsCache(session=Boom(), persist=False)
    assert cache.allowed("http://a.test/x") is True


def test_robots_crawl_delay_parsed():
    text = "User-agent: *\nCrawl-delay: 5\nDisallow: /x\n"
    cache = politeness.RobotsCache(session=FakeRobotsSession(200, text), persist=False)
    assert cache.crawl_delay("http://a.test/anything") == 5.0


def test_robots_cache_persists_across_instances(tmp_path):
    path = tmp_path / "robots.json"
    text = "User-agent: *\nDisallow: /private\n"
    first = politeness.RobotsCache(cache_path=str(path), session=FakeRobotsSession(200, text), persist=True)
    assert first.allowed("http://a.test/private/x") is False
    assert path.exists()

    class Boom:
        def get(self, *a, **k):
            raise AssertionError("robots.txt should be served from the persisted cache")

    second = politeness.RobotsCache(cache_path=str(path), session=Boom(), persist=True)
    assert second.allowed("http://a.test/private/x") is False
    assert second.crawl_delay("http://a.test/x") is None


def test_robots_cache_ttl_expiry_refetches(tmp_path):
    path = tmp_path / "robots.json"
    stale = {"_version": 1, "entries": {"http://a.test": {
        "ts": _time.time() - 10 * 86400, "status": 200,
        "rules": [["disallow", "/old"]], "crawl_delay": 4.0}}}
    path.write_text(json.dumps(stale), encoding="utf-8")
    session = FakeRobotsSession(200, "User-agent: *\nAllow: /\n")
    cache = politeness.RobotsCache(cache_path=str(path), session=session, persist=True, ttl=24 * 3600)
    assert cache.allowed("http://a.test/old/x") is True   # TTL 过期后重新抓，不再用旧缓存
    assert session.calls
    assert cache.crawl_delay("http://a.test/x") is None


def test_robots_persist_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOTS_CACHE", "0")
    path = tmp_path / "robots.json"
    cache = politeness.RobotsCache(cache_path=str(path), session=FakeRobotsSession(200, "User-agent: *\nDisallow: /\n"))
    assert cache.persist is False
    assert cache.allowed("http://a.test/x") is False
    assert not path.exists()


# ---------- Crawl-delay -> 每域最小间隔 ----------
def test_domain_limiter_honours_crawl_delay(monkeypatch):
    limiter = politeness.DomainLimiter(rate=100.0)
    sleeps = []
    monkeypatch.setattr(politeness.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(politeness.time, "monotonic", lambda: 1000.0)
    limiter.acquire("http://slow.test/a", min_interval=3.0)
    assert sleeps == []
    limiter.acquire("http://slow.test/b", min_interval=3.0)
    assert len(sleeps) == 1 and abs(sleeps[0] - 3.0) < 1e-6


# ---------- robots 按站覆盖（sources.yaml: robots: false） ----------
def test_stream_many_blocks_robots_by_default(monkeypatch):
    monkeypatch.setattr(web.ROBOTS, "allowed", lambda u: False)
    monkeypatch.setattr(web.ROBOTS, "crawl_delay", lambda u: 5.0)
    monkeypatch.setattr(web, "get_text", lambda url, headers=None, timeout=None: (200, "<html>ok</html>"))
    fetcher = web.Fetcher({"domain": "a.test", "base": "https://a.test"})
    assert list(fetcher.stream_many(["https://a.test/x"], workers=2)) == [("https://a.test/x", -1, "robots")]


def test_stream_many_robots_false_still_fetches(monkeypatch):
    monkeypatch.setattr(web.ROBOTS, "allowed", lambda u: False)
    monkeypatch.setattr(web.ROBOTS, "crawl_delay", lambda u: 5.0)
    monkeypatch.setattr(web, "get_text", lambda url, headers=None, timeout=None: (200, "<html>ok</html>"))
    fetcher = web.Fetcher({"domain": "a.test", "base": "https://a.test", "robots": False})
    assert list(fetcher.stream_many(["https://a.test/x"], workers=2)) == [("https://a.test/x", 200, "<html>ok</html>")]


# ---------- via: playwright（服务器浏览器路径） ----------
def test_fetcher_playwright_branch(monkeypatch):
    import crawler.browser_playwright as pw

    seen = {}

    def fake(urls, wait_ms=None, headless=True, block_resources=False, user_agent=None, ignore_https_errors=False):
        seen.update(wait_ms=wait_ms, headless=headless, block=block_resources, ua=user_agent)
        return [(u, 200, "<html>ok</html>") for u in urls]

    monkeypatch.setattr(pw, "fetch_many", fake)
    f = web.Fetcher({"domain": "a.test", "base": "https://a.test", "via": "playwright",
                     "playwright_wait_ms": 2500, "block_resources": True})
    out = f.get_many(["https://a.test/x"])
    assert out == [("https://a.test/x", 200, "<html>ok</html>")]
    assert seen["wait_ms"] == 2500 and seen["block"] is True and seen["ua"]


def test_playwright_config_validates():
    from crawler import schema
    site = {"domain": "p.test", "channel": "html", "base": "https://p.test",
            "list_urls": ["/"], "via": "playwright", "playwright_wait_ms": 3000,
            "block_resources": True, "headless": True}
    errors, _warnings = schema.validate_site(site)
    assert errors == []


def test_https_exception_is_scoped_to_one_fetcher(fake_get, monkeypatch):
    monkeypatch.setattr(web.LIMITER, 'acquire', lambda *a, **k: None)
    relaxed = web.Fetcher({'domain': 'expired.test', 'robots': False, 'ignore_https_errors': True})
    ordinary = web.Fetcher({'domain': 'normal.test', 'robots': False})
    relaxed.get_many(['https://expired.test/a'])
    ordinary.get_many(['https://normal.test/b'])
    assert fake_get.calls[0][1]['verify'] is False
    assert fake_get.calls[1][1]['verify'] is True
