"""endpoints 挖接口与 detect() 挂钩的离线测试：只用内置夹具 + 127.0.0.1 http.server。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from crawler import adhoc, endpoints

PAGE_HTML = """<html><head><title>Community home</title>
<script src="/static/app.js"></script>
<script src="https://cdn.example.net/vendor.js"></script>
<script>const a = await fetch("/api/v2/articles?page=1");</script>
</head><body><a href="/about">About</a><img src="/logo.png"></body></html>"""

APP_JS = """
fetch("/api/v2/articles");
axios.get("/api/v2/articles/42/comments");
$.getJSON("/api/v2/tags");
var x = new XMLHttpRequest(); x.open("GET", "/api/v2/search?q=1");
const g = "/graphql";
const nd = "/_next/data/abc123def456/news.json";
const r = "?rest_route=/wp/v2/posts&per_page=5";
var img = "/static/logo.png";
var css = "/assets/main.css";
var home = "/about";
var mail = "mailto:editor@example.test";
"""


class FakeFetcher:
    """只要 stream_many(urls) 形状：(url, status, body)。"""

    def __init__(self, bodies=None, site=None):
        self.bodies = bodies or {}
        self.site = site or {}
        self.requested = []

    def stream_many(self, urls, workers=4):
        for url in urls:
            self.requested.append(url)
            yield url, 200, self.bodies.get(url, "")


def _urls(candidates):
    return [c["url"] for c in candidates]


# ---------- extract ----------

def test_extract_finds_api_next_data_graphql():
    found = endpoints.extract(APP_JS, "https://example.test/", source="app.js")
    urls = _urls(found)
    assert "https://example.test/api/v2/articles" in urls
    assert "https://example.test/_next/data/abc123def456/news.json" in urls
    assert "https://example.test/graphql" in urls
    kinds = {c["url"]: c["kind"] for c in found}
    assert kinds["https://example.test/api/v2/articles"] == "api"
    assert kinds["https://example.test/_next/data/abc123def456/news.json"] == "next_data"
    assert kinds["https://example.test/graphql"] == "graphql"
    assert all(c["source"] == "app.js" and c["score"] > 0 for c in found)


def test_extract_rest_route_kind():
    found = endpoints.extract(APP_JS, "https://example.test/")
    rest = [c for c in found if c["kind"] == "rest_route"]
    assert rest and "rest_route=/wp/v2/posts" in rest[0]["url"]


def test_extract_filters_assets_and_noise():
    urls = _urls(endpoints.extract(APP_JS + PAGE_HTML, "https://example.test/"))
    for junk in (".png", ".css", "/about", "mailto:"):
        assert not any(junk in url for url in urls), junk


def test_extract_dedupes_and_merges_reasons():
    text = 'fetch("/api/v2/articles"); var other = "/api/v2/articles";'
    found = endpoints.extract(text, "https://example.test/")
    assert len([u for u in _urls(found) if u.endswith("/api/v2/articles")]) == 1
    assert "call" in found[0]["reasons"]


def test_extract_ignores_mime_types_and_bare_tokens():
    js = 'var j = "application/json"; var m = "GET"; fetch("/api/v2/x");'
    assert _urls(endpoints.extract(js, "https://example.test/")) == ["https://example.test/api/v2/x"]


def test_extract_handles_empty_input():
    assert endpoints.extract("", "https://example.test/") == []
    assert endpoints.extract(None, "https://example.test/") == []


# ---------- scan ----------

def test_scan_inline_only_without_fetcher():
    result = endpoints.scan(PAGE_HTML, "https://example.test/")
    assert result["via"] == "python"
    assert result["scripts"] == 1                       # 只有一段内联脚本
    assert "https://example.test/api/v2/articles?page=1" in _urls(result["candidates"])


def test_scan_fetches_external_scripts_with_fake_fetcher():
    fetcher = FakeFetcher({"https://example.test/static/app.js": APP_JS})
    result = endpoints.scan(PAGE_HTML, "https://example.test/", fetcher=fetcher)
    assert result["scripts"] == 2
    assert "https://example.test/static/app.js" in fetcher.requested
    # 只有外链 bundle 里才有的接口
    assert "https://example.test/api/v2/articles/42/comments" in _urls(result["candidates"])


def test_scan_public_host_limits_to_same_site():
    fetcher = FakeFetcher(site={"_public_host": "example.test"})
    endpoints.scan(PAGE_HTML, "https://example.test/", fetcher=fetcher)
    assert all(urlsplit(u).netloc == "example.test" for u in fetcher.requested)
    assert "https://cdn.example.net/vendor.js" not in fetcher.requested


def test_scan_respects_limit():
    html = "".join(f'<script src="/s{i}.js"></script>' for i in range(5))
    fetcher = FakeFetcher()
    endpoints.scan(html, "https://example.test/", fetcher=fetcher, limit=2)
    assert len(fetcher.requested) == 2


def test_scan_survives_fetcher_errors():
    class Boom(FakeFetcher):
        def stream_many(self, urls, workers=4):
            raise RuntimeError("boom")

    result = endpoints.scan(PAGE_HTML, "https://example.test/", fetcher=Boom())
    assert "https://example.test/api/v2/articles?page=1" in _urls(result["candidates"])


# ---------- rank / suggest ----------

def test_rank_orders_by_score_then_depth():
    candidates = [
        {"kind": "api", "url": "https://example.test/api/v2/a/b/c", "score": 60, "reasons": []},
        {"kind": "api", "url": "https://example.test/api/v2/a", "score": 60, "reasons": []},
        {"kind": "api", "url": "https://example.test/api/v2/z", "score": 80, "reasons": []},
    ]
    ranked = endpoints.rank(candidates)
    assert [c["url"] for c in ranked] == ["https://example.test/api/v2/z",
                                          "https://example.test/api/v2/a",
                                          "https://example.test/api/v2/a/b/c"]
    assert endpoints.rank(candidates) == ranked                 # 稳定


def test_suggest_returns_usable_fragment():
    found = endpoints.extract(APP_JS, "https://example.test/")
    entry = endpoints.suggest(found)
    assert entry and entry["channel"] in ("json-api", "json-list")
    assert entry["channel"] == "json-list"
    assert entry["api_list"] == "/api/v2/articles?page={page}"
    # 片段必须能被 json-list 的模板格式化
    assert entry["api_list"].format(page=2) == "/api/v2/articles?page=2"
    assert entry["confidence"] >= endpoints.SUGGEST_MIN


def test_suggest_skips_weak_or_non_api_candidates():
    weak = [{"kind": "api", "url": "https://example.test/api/v2/x", "score": 10, "reasons": []}]
    assert endpoints.suggest(weak) is None
    plain = [{"kind": "path", "url": "https://example.test/search", "score": 90, "reasons": []}]
    assert endpoints.suggest(plain) is None
    assert endpoints.suggest([]) is None


def test_suggest_graphql_uses_json_api():
    found = [{"kind": "graphql", "url": "https://example.test/graphql", "score": 70, "reasons": []}]
    entry = endpoints.suggest(found)
    assert entry["channel"] == "json-api" and entry["api"] == "/graphql"


# ---------- jsluice 降级 ----------

def test_jsluice_absent_returns_empty(monkeypatch):
    monkeypatch.setattr(endpoints.shutil, "which", lambda name: None)
    assert endpoints.jsluice_extract(["/tmp/does-not-matter.js"]) == []


def test_jsluice_results_merge_into_scan(monkeypatch):
    monkeypatch.setattr(endpoints, "_jsluice_available", lambda: True)
    lines = "\n".join([
        json.dumps({"url": "/api/v2/from-jsluice", "info": {"type": "fetch"}}),
        json.dumps({"url": "https://example.test/logo.png"}),
        "not json",
    ])
    monkeypatch.setattr(endpoints, "_run_jsluice", lambda path: lines)
    result = endpoints.scan(PAGE_HTML, "https://example.test/")
    assert result["via"] == "jsluice"
    assert "https://example.test/api/v2/from-jsluice" in _urls(result["candidates"])
    assert not any("logo.png" in url for url in _urls(result["candidates"]))
    item = next(c for c in result["candidates"] if c["url"].endswith("from-jsluice"))
    assert item["source"] == "jsluice" and "jsluice" in item["reasons"]


# ---------- detect() 集成 ----------

HOME_HTML = """<html><head><title>Community home</title>
<script src="/static/app.js"></script>
<script>const a = await fetch("/api/v2/articles?page=1");</script>
</head><body><h1>Home</h1><a href="/news/">News</a></body></html>"""


class Source(BaseHTTPRequestHandler):
    """只服务站点首页 + 一篇 bundle；wp-json/sitemap/feed 全 404，逼 detect 走 html 兜底。"""

    def log_message(self, *args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        query = parts.query
        status, ctype, body = 404, "text/plain", "not found"
        if parts.path == "/robots.txt":
            status, body = 200, "User-agent: *\nAllow: /"
        elif parts.path == "/wp-json/wp/v2/posts" and "per_page=1" in query:
            status, body = 404, '{"code":"rest_no_route"}'
        elif parts.path == "/" and "rest_route=" in query:
            status, body = 404, '{"code":"rest_no_route"}'
        elif parts.path == "/sitemap.xml":
            status, body = 404, "no sitemap"
        elif parts.path == "/" and not query:
            status, ctype, body = 200, "text/html; charset=utf-8", HOME_HTML
        elif parts.path == "/static/app.js":
            status, ctype, body = 200, "text/javascript", 'axios.get("/api/v2/comments");'
        elif parts.path.startswith(("/feed", "/rss", "/atom", "/index.php")):
            status, body = 404, "no feed"
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def source():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _allow_private(monkeypatch):
    # 127.0.0.1 上的夹具站点要过 normalize_url 的私网/端口检查
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE", "1")
    monkeypatch.delenv("ENDPOINT_AUTOCONFIG", raising=False)
    # 默认按「本机没有 jsluice」跑，需要增强分支的用例自己再打开
    monkeypatch.setattr(endpoints, "_jsluice_available", lambda: False)


def test_detect_keeps_channel_and_attaches_endpoints(source):
    site = adhoc.detect(source + "/")
    assert site["channel"] == "html"                       # 通道语义不变
    assert site["list_url"].startswith(source)
    urls = [c["url"] for c in site["detected"]["endpoints"]]
    assert urls and "http://" in urls[0]
    assert any("/api/v2/articles" in url for url in urls)
    assert site["detected"]["suggested"]["channel"] == "json-list"
    assert "api_list" not in site                          # 默认不落配置
    assert len(site["detected"]["endpoints"]) <= 10


def test_legacy_autoconfig_flag_does_not_change_channel(source, monkeypatch):
    monkeypatch.setenv("ENDPOINT_AUTOCONFIG", "1")
    site = adhoc.detect(source + "/")
    assert site["channel"] == "html"
    assert "api_list" not in site
    assert site["domain"].startswith("127.0.0.1")
    assert site["detected"]["suggested"]["channel"] == "json-list"


def test_detect_scan_never_fetches_cross_site(monkeypatch):
    """public=True 时 scan 只碰本站脚本：用假 fetcher 断言请求集合。"""
    fetcher = FakeFetcher(site={"_public_host": "example.test"})
    endpoints.scan(PAGE_HTML, "https://example.test/", fetcher=fetcher)
    assert fetcher.requested == ["https://example.test/static/app.js"]


def test_cross_site_literals_are_dropped():
    """内联脚本里常引用第三方（google.com/ccm/geo），跨站 URL 不该进候选。"""
    js = 'fetch("https://www.google.com/ccm/geo");fetch("/api/v2/articles");'
    found = endpoints.extract(js, "https://news.example.test/")
    urls = [c["url"] for c in found]
    assert all("google.com" not in url for url in urls), urls
    assert any("/api/v2/articles" in url for url in urls)


def test_noise_paths_score_lower_than_content_api():
    """埋点/会话类接口降分，WP REST 根与内容空间加分——否则建议位会被 track 抢走。"""
    js = ('fetch("/wp-json/");fetch("/wp-json/woocommerce-analytics/v1/track");'
          'fetch("/wp-json/wp/v2/posts");')
    found = endpoints.extract(js, "https://news.example.test/")
    by_path = {c["url"].split("example.test")[-1]: c for c in found}
    root = by_path["/wp-json/"]
    track = by_path["/wp-json/woocommerce-analytics/v1/track"]
    posts = by_path["/wp-json/wp/v2/posts"]
    assert "content-api" in root["reasons"] and "noise" in track["reasons"]
    assert root["score"] > track["score"] and posts["score"] > track["score"]
    assert track["score"] < endpoints.SUGGEST_MIN


def test_suggest_skips_index_and_noise():
    """接口目录（/wp-json、/api）与埋点接口不给配置；内容接口才给 json-list。"""
    noise = [{"kind": "api", "url": "https://x.test/wp-json/woocommerce-analytics/v1/track",
              "score": 90, "reasons": ["noise", "api"]}]
    index = [{"kind": "api", "url": "https://x.test/wp-json/", "score": 90, "reasons": ["content-api"]}]
    content = [{"kind": "api", "url": "https://x.test/wp-json/wp/v2/posts", "score": 90,
                "reasons": ["content-api"]}]
    assert endpoints.suggest(noise) is None
    assert endpoints.suggest(index) is None
    entry = endpoints.suggest(content)
    # WP 内容接口直接给 wp-api，比猜 json-list 的 api_items 更靠谱
    assert entry and entry["channel"] == "wp-api" and entry["api"] == "wp-json"


def test_next_build_id_is_folded_into_data_urls():
    """Next.js 的 /_next/data/${buildId}/ 占位要换成页面里的真实 buildId。"""
    html = ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            '{"buildId":"93rGSos_KsGher1rYca2v","props":{}}</script></body></html>')
    js = 'fetch(`/_next/data/${buildId}/news/index.json`);'
    scanned = endpoints.scan(html, "https://next.example.test/")

    class Fetcher:
        site = {}

        def stream_many(self, urls):
            for url in urls:
                yield url, 200, js

    scanned = endpoints.scan(html + '<script src="/app.js"></script>', "https://next.example.test/",
                             fetcher=Fetcher())
    urls = [c["url"] for c in scanned["candidates"]]
    assert scanned["build_id"] == "93rGSos_KsGher1rYca2v"
    assert any("93rGSos_KsGher1rYca2v" in url for url in urls), urls
    assert all("${" not in url for url in urls), urls


def test_wp_json_root_is_expanded_into_content_endpoints():
    """看到 /wp-json 根就顺手读 routes，直接给出 wp/v2 内容接口。"""
    html = '<html><body><script>fetch("/wp-json/")</script></body></html>'
    routes = {"routes": {"/wp/v2/posts": {}, "/wp/v2/pages": {}, "/wp/v2/media": {},
                         "/wp/v2/blocks": {}}}

    class Fetcher:
        site = {}

        def stream_many(self, urls):
            for url in urls:
                if url.rstrip("/").endswith("/wp-json"):
                    yield url, 200, json.dumps(routes)
                else:
                    yield url, 404, ""

    scanned = endpoints.scan(html, "https://wp.example.test/", fetcher=Fetcher())
    urls = {c["url"] for c in scanned["candidates"]}
    assert "https://wp.example.test/wp-json/wp/v2/posts" in urls
    assert "https://wp.example.test/wp-json/wp/v2/pages" in urls
    assert not [u for u in urls if u.endswith("/wp/v2/blocks")], "只推常见内容类型"
    suggestion = endpoints.suggest(scanned["candidates"])
    assert suggestion and suggestion["channel"] == "wp-api"
