"""contract / capture 与预览集成：全离线（内置 HAR 夹具 + 127.0.0.1 http.server）。"""
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

import crawler
import web_preview
from crawler import capture, contract, schema

# 预览的既有返回结构（capture_contract=False 时必须一模一样，不许多键）
PREVIEW_KEYS = {"url", "channel", "transport", "source", "samples", "issues", "editable",
                "rules", "preview_id", "available_kinds", "selection"}
ROBOTS = "User-agent: *\nAllow: /\n"


def _articles(page):
    """分页 JSON：3 条文章 + 列表键 items + 嵌套 meta。"""
    rows = [{"id": index, "title": f"Report {index}", "slug": f"report-{index}",
             "published_at": "2026-11-01T00:00:00Z", "url": f"https://example.test/story/{index}"}
            for index in range((page - 1) * 3 + 1, page * 3 + 1)]
    return json.dumps({"items": rows, "page": page, "total": 42,
                       "meta": {"total": 42, "per_page": 3}}, ensure_ascii=False)


def _har_entry(url, text, status=200, mime="application/json", method="GET",
               started="2026-11-25T10:00:00.000Z", resource="xhr"):
    return {
        "startedDateTime": started,
        "time": 12.5,
        "_resourceType": resource,
        "request": {"method": method, "url": url, "headers": [{"name": "accept", "value": mime}]},
        "response": {"status": status, "headers": [{"name": "content-type", "value": mime}],
                     "content": {"mimeType": mime, "size": len(text.encode()), "text": text}},
    }


@pytest.fixture
def har():
    """两条 /api/v2/articles?page=1|2 的抓包夹具（离线、可直接复用）。"""
    return {"log": {"version": "1.2", "creator": {"name": "fixture", "version": "1"},
                    "entries": [
                        _har_entry("https://example.test/api/v2/articles?page=1", _articles(1),
                                   started="2026-11-25T10:00:00.000Z"),
                        _har_entry("https://example.test/api/v2/articles?page=2", _articles(2),
                                   started="2026-11-25T10:02:00.000Z"),
                    ]}}


class Site(BaseHTTPRequestHandler):
    """本地夹具站：首页 + 详情页 + 分页 JSON 接口。"""
    def log_message(self, *args):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        status, body, mime = 200, "", "text/html; charset=utf-8"
        if parts.path == "/robots.txt":
            body = ROBOTS
        elif parts.path == "/":
            body = ('<html><head><title>Index</title></head><body>'
                    '<a href="/story/1">Story 1</a><a href="/story/2">Story 2</a>'
                    '<script src="/app.js"></script></body></html>')
        elif parts.path == "/app.js":
            body, mime = 'fetch("/api/v2/articles?page=1")', "application/javascript"
        elif parts.path.startswith("/story/"):
            body = ('<html><head><title>Story</title></head><body><article><h1>Story</h1>'
                    + '<p>Residents share detailed information about local community services.</p>' * 6
                    + '</article></body></html>')
        elif parts.path == "/api/v2/articles":
            body, mime = _articles(int(query.get("page", ["1"])[0])), "application/json"
        else:
            status, body = 404, "not found"
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------- contract

def test_from_har_clusters_infers_pagination_fields_and_score(har):
    candidates = contract.from_har(har)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["endpoint"] == "GET example.test/api/v2/articles"
    assert candidate["method"] == "GET" and candidate["host"] == "example.test"
    assert candidate["path_template"] == "/api/v2/articles"
    assert candidate["sample_count"] == 2
    assert candidate["pagination"] == {"param": "page", "values": [1, 2], "confidence": 0.8}
    assert candidate["query"]["page"] == ["1", "2"]
    assert candidate["channel"] == "json-list"
    # 字段：顶层键 + 一层嵌套（对象 meta.total、数组 items[].title）+ 完整度
    assert candidate["fields"]["items"] == {"type": "list", "completeness": 1.0}
    assert candidate["fields"]["meta.total"]["completeness"] == 1.0
    assert candidate["fields"]["items[].title"]["completeness"] == 1.0
    assert 0 < candidate["score"] <= 100
    assert any("分页参数 page" in reason for reason in candidate["reasons"])
    assert candidate["evidence"]["status_codes"] == [200]
    assert candidate["evidence"]["time_range"] == ["2026-11-25T10:00:00.000Z",
                                                   "2026-11-25T10:02:00.000Z"]
    assert candidate["evidence"]["sample_url"].endswith("?page=1")


def test_from_har_filters_hosts_non_api_and_small_bodies(har):
    har["log"]["entries"].append(_har_entry("https://other.test/api/v2/articles?page=1", _articles(1)))
    har["log"]["entries"].append(_har_entry("https://example.test/api/v2/tiny?page=1", '{"a":1}'))
    har["log"]["entries"].append(_har_entry("https://example.test/page", "<html>doc</html>",
                                            mime="text/html", resource="document"))
    # 默认收全部主机；小响应与文档页面被 min_bytes / 非接口过滤掉
    assert sorted(item["host"] for item in contract.from_har(har)) == ["example.test", "other.test"]
    only_other = contract.from_har(har, hosts=["other.test"])
    assert [item["host"] for item in only_other] == ["other.test"]
    assert [item["path_template"] for item in contract.from_har(har, hosts=["example.test"])] == \
        ["/api/v2/articles"]
    # min_bytes 放宽后小响应才进来
    tiny = [item for item in contract.from_har(har, min_bytes=1) if "tiny" in item["path_template"]]
    assert len(tiny) == 1


def test_path_template_collapses_numbers_uuids_and_hashes():
    assert contract.path_template("/api/v2/articles/123/comments") == "/api/v2/articles/{id}/comments"
    assert contract.path_template("/x/1.json") == "/x/{id}.json"
    assert contract.path_template("/x/550e8400-e29b-41d4-a716-446655440000") == "/x/{id}"
    assert contract.path_template("/x/deadbeefdeadbeefdeadbeef") == "/x/{id}"
    assert contract.path_template("/news/") == "/news/"


def test_to_sources_entry_is_valid_sources_yaml(har):
    candidate = contract.from_har(har)[0]
    entry = contract.to_sources_entry(candidate)
    errors, _warnings = schema.validate_site(entry)
    assert errors == []
    assert entry["domain"] == "example.test"
    assert entry["base"] == "https://example.test"
    assert entry["channel"] == "json-list"
    assert entry["api_list"] == "/api/v2/articles?page={page}"
    assert entry["api_url"].startswith("{base}")
    assert "{id}" in entry["api_url"]
    # 无分页的候选走 json-api，api_items 取最像数组的键
    plain = dict(candidate, pagination=None, channel="json-api")
    api_entry = contract.to_sources_entry(plain, domain="example.test")
    assert api_entry["channel"] == "json-api" and api_entry["api_items"] == "items"
    assert schema.validate_site(api_entry)[0] == []


def test_from_endpoints_maps_kind_and_keeps_template():
    rows = contract.from_endpoints([
        {"kind": "json-list", "url": "https://example.test/api/v2/articles?page=1", "score": 0.9},
        {"kind": "api", "url": "https://example.test/api/v2/authors/42", "score": 60},
        {"url": "/relative/only"},
        "not-a-dict",
    ])
    assert len(rows) == 2
    assert {row["path_template"] for row in rows} == {"/api/v2/articles", "/api/v2/authors/{id}"}
    assert rows[0]["channel"] == "json-list" and rows[1]["channel"] == "json-api"
    assert all(row["fields"] == {} and row["pagination"] is None for row in rows)
    assert all(0 < row["score"] <= 70 for row in rows)
    assert rows[0]["query"]["page"] == ["1"]
    assert schema.validate_site(contract.to_sources_entry(rows[1]))[0] == []


def test_render_outputs_readable_table(har):
    text = contract.render(contract.from_har(har))
    lines = text.splitlines()
    assert lines[0].startswith("| # | 接口 | 分页 | 字段 |")
    assert "example.test/api/v2/articles" in text
    assert "page 1→2" in text
    assert "items(list)" in text
    assert contract.render([]).startswith("（没有可用的接口候选")


# ----------------------------------------------------------------- capture

class _Request:
    def __init__(self, url, resource_type="xhr", method="GET"):
        self.url, self.resource_type, self.method = url, resource_type, method
        self.headers = {"accept": "application/json"}


class _Response:
    def __init__(self, url, body, status=200, mime="application/json", resource_type="xhr"):
        self.request = _Request(url, resource_type=resource_type)
        self.status, self.headers = status, {"content-type": mime}
        self._body = body

    def text(self):
        return self._body


class _Page:
    def __init__(self, responses):
        self._responses, self._handlers, self.closed = responses, [], False

    def on(self, event, handler):
        assert event == "response"
        self._handlers.append(handler)

    def goto(self, url, wait_until=None, timeout=None):
        for response in self._responses:
            for handler in self._handlers:
                handler(response)

    def wait_for_timeout(self, _ms):
        pass

    def click(self, selector, timeout=None):
        pass

    def close(self):
        self.closed = True


class _Context:
    def __init__(self, responses):
        self._responses, self.page = responses, None

    def new_page(self):
        self.page = _Page(self._responses)
        return self.page


def test_capture_available_follows_importable_backend(monkeypatch):
    assert isinstance(capture.available(), bool)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    assert capture.available() is False
    with pytest.raises(RuntimeError, match="playwright"):
        capture.capture("https://example.test/")


def test_capture_builds_har_and_truncates(monkeypatch, tmp_path):
    context = _Context([
        _Response("https://example.test/api/v2/articles?page=1", _articles(1)),
        _Response("https://example.test/logo.png", "PNGDATA", mime="image/png",
                  resource_type="image"),
        _Response("https://example.test/api/v2/plain", "plain text payload", mime="text/plain",
                  resource_type="fetch"),
    ])
    monkeypatch.setattr(capture, "_sync_api", object)
    monkeypatch.setattr(capture, "_context", lambda headless=True: context)
    har = capture.capture("https://example.test/", wait_ms=10, max_body=60)
    assert har["log"]["version"] == "1.2"
    entries = har["log"]["entries"]
    assert len(entries) == 2                       # 图片响应被过滤
    assert entries[0]["request"]["method"] == "GET"
    assert entries[0]["response"]["content"]["mimeType"] == "application/json"
    assert len(entries[0]["response"]["content"]["text"]) == 60
    assert entries[0]["response"]["content"]["truncated"] is True
    assert entries[1]["request"]["url"].endswith("/api/v2/plain")
    assert context.page.closed is True
    # 抓到的 HAR 能直接喂给契约推断
    assert contract.from_har(har, min_bytes=1)
    path = capture.save(har, tmp_path / "capture" / "har.json")
    assert json.loads(open(path, encoding="utf-8").read())["log"]["version"] == "1.2"


# ----------------------------------------------------------------- preview

def _fake_site(base):
    return {"domain": urlsplit(base).netloc, "base": base, "channel": "html",
            "via": "direct", "start_url": base + "/", "discover_cap": 0}


class _Fetcher:
    """离线 fetcher：任何详情页都返回一份够长的正文。"""
    def __init__(self, site):
        self.site = site

    def get_many(self, urls, timeout=12):
        return [(url, 200, '<html><body><article><h1>Story</h1><p>'
                 + "Residents share detailed information about local community services. " * 8
                 + "</p></article></body></html>") for url in urls]

    def stream_many(self, urls, workers=1):
        yield from self.get_many(urls)


@pytest.fixture
def offline_preview(monkeypatch, site):
    """把预览的发请求部分全部打桩：站点解析、发现、页面抓取、正文抽取。"""
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE", "1")
    base = site
    monkeypatch.setattr(web_preview, "resolve_site", lambda url, **kwargs: _fake_site(base))
    monkeypatch.setattr(web_preview, "Fetcher", _Fetcher)
    monkeypatch.setattr(web_preview, "SampleFetcher", _Fetcher)
    monkeypatch.setattr(web_preview.discover, "html_urls", lambda *args, **kwargs: [base + "/story/1"])
    monkeypatch.setattr(web_preview, "extract_best", lambda url, body, site, min_text=0: {
        "url": url, "title": "Story", "created_at": "2026-11-01",
        "text": "Residents share detailed information about local community services. " * 8})
    return base


def test_preview_default_keeps_exact_structure(offline_preview):
    result = web_preview.preview(offline_preview + "/", {})
    assert set(result) == PREVIEW_KEYS
    assert "candidates" not in result and "capture_note" not in result
    assert result["samples"] and result["preview_id"]


def test_preview_with_har_exposes_candidates(offline_preview, har):
    result = web_preview.preview(offline_preview + "/", {}, capture_contract=True, har=har)
    assert set(result) == PREVIEW_KEYS | {"candidates", "capture_note"}
    assert result["capture_note"] == "har"
    assert result["candidates"][0]["path_template"] == "/api/v2/articles"
    assert len(result["candidates"]) <= 5
    # 预览的既有字段不受影响
    assert result["samples"] and result["preview_id"]


def test_preview_falls_back_to_html_endpoints(offline_preview, monkeypatch):
    seen = []

    def scan(html, base_url, fetcher=None, limit=12):
        seen.append((html, base_url))
        return {"candidates": [
            {"kind": "json-list", "url": base_url.rstrip("/") + "/api/v2/articles?page=1",
             "score": 0.8, "source": "inline"}]}

    module = types.ModuleType("crawler.endpoints")
    module.scan = scan
    monkeypatch.setitem(sys.modules, "crawler.endpoints", module)
    monkeypatch.setattr(crawler, "endpoints", module, raising=False)
    monkeypatch.setattr(capture, "available", lambda: False)

    result = web_preview.preview(offline_preview + "/", {}, capture_contract=True)
    assert result["capture_note"] == "html"
    assert result["candidates"][0]["channel"] == "json-list"
    assert seen and "app.js" in seen[0][0]          # 首页 HTML 确实被取到并扫描
    assert seen[0][1] == offline_preview + "/"


def test_script_fetcher_only_takes_same_host_scripts(offline_preview):
    fetcher = web_preview._ScriptFetcher(offline_preview + "/")
    rows = list(fetcher.stream_many([offline_preview + "/app.js", "https://cdn.other.test/app.js"]))
    assert rows == [(offline_preview + "/app.js", 200, 'fetch("/api/v2/articles?page=1")')]
    assert fetcher.site["_public_host"] == urlsplit(offline_preview).netloc


def test_preview_capture_unavailable_still_returns_contract_keys(offline_preview, monkeypatch):
    # 模拟 T2 模块缺失：包属性与 sys.modules 都要摘，否则前面用例 import 过的模块会从缓存命中
    import crawler
    monkeypatch.delattr(crawler, "endpoints", raising=False)
    monkeypatch.setitem(sys.modules, "crawler.endpoints", None)
    monkeypatch.setattr(capture, "available", lambda: False)
    result = web_preview.preview(offline_preview + "/", {}, capture_contract=True)
    assert result["candidates"] == []
    assert result["capture_note"] == "unavailable"
