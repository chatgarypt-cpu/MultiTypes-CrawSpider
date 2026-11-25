"""离线验收：公开接口识别、HAR 推断、试采与信源配置。"""
import inspect
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import yaml

APP_JS = """
const list = fetch("/api/v2/articles?page=1");
axios.get("/api/v2/articles", {params: {page: 2}});
const gql = "/graphql";
const next = "/_next/data/build-abc/news.json";
const decoy = "/static/logo.png";
const css = "/static/app.css";
"""

HOME = """<!doctype html><html><head><title>夹具站</title></head><body>
<h1>fixture</h1>
<script src="/static/app.js"></script>
<script>fetch("/api/v2/articles?page=1").then(r => r.json());</script>
</body></html>"""


def _articles(page=1):
    return {"data": [{"id": int(page), "title": f"article {page}", "url": f"/a/{page}",
                      "published_at": "2026-11-01"}],
            "meta": {"total": 2}}


class FixtureHandler(BaseHTTPRequestHandler):
    """公开接口的本地夹具。"""

    protocol_version = "HTTP/1.0"   # 每条请求关连接：避免 keep-alive 收尾时的 RST 噪声

    def log_message(self, *args):        # 测试输出保持干净
        pass

    def _send(self, status, body, ctype="text/html; charset=utf-8"):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        if path == "/robots.txt":
            return self._send(200, "User-agent: *\nAllow: /\n", "text/plain")
        if path == "/":
            return self._send(200, HOME)
        if path == "/static/app.js":
            return self._send(200, APP_JS, "application/javascript")
        if path == "/static/logo.png":
            return self._send(200, b"\x89PNG\r\n", "image/png")
        if path == "/api/v2/articles":
            return self._send(200, json.dumps(_articles((query.get("page") or ["1"])[0])),
                              "application/json")
        if path == "/health":
            return self._send(200, json.dumps({"msg": "ok"}), "application/json")
        return self._send(404, "not found", "text/plain")



@pytest.fixture(scope="module")
def fixture_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def host(fixture_site):
    return urlsplit(fixture_site).netloc


class ScriptFetcher:
    """只提供 stream_many 形状的假 fetcher（endpoints.scan 的鸭子类型入口）。"""

    def __init__(self, base):
        self.base = base
        self.requested = []

    def stream_many(self, urls):
        for url in urls:
            self.requested.append(url)
            if url.endswith("/static/app.js"):
                yield url, 200, APP_JS
            else:
                yield url, 404, ""


# ---------- 1. 挖接口 → 契约 → 合法配置 ----------

def test_endpoints_to_sources_entry_passes_schema(fixture_site):
    from crawler import contract, endpoints, schema

    home = requests.get(fixture_site + "/", timeout=10).text
    inline = endpoints.scan(home, fixture_site + "/")
    assert [c for c in inline["candidates"] if "/api/v2/articles" in c["url"]], \
        "内联脚本里的 fetch('/api/v2/articles?...') 应该被挖到"

    fetcher = ScriptFetcher(fixture_site)
    scanned = endpoints.scan(home, fixture_site + "/", fetcher=fetcher)
    assert fetcher.requested and all(u.startswith(fixture_site) for u in fetcher.requested), \
        "scan 只应该拉同站脚本"
    kinds = {c["kind"] for c in scanned["candidates"]}
    assert {"next_data", "graphql"} <= kinds, f"外链脚本里的接口没挖到: {kinds}"
    assert not [c for c in scanned["candidates"]
                if c["url"].endswith((".png", ".css"))], "静态资源不应成为候选"

    ranked = endpoints.rank(scanned["candidates"])
    assert ranked[0]["score"] == max(c["score"] for c in ranked), "rank 应按分数降序"

    candidates = contract.from_endpoints(ranked)
    assert candidates and candidates[0].get("host") == urlsplit(fixture_site).netloc

    entry = contract.to_sources_entry(candidates[0], domain="acceptance.test")
    assert entry["channel"] in ("json-api", "json-list")
    errors, _warnings = schema.validate_site({**entry, "base": entry.get("base") or fixture_site})
    assert errors == [], f"生成的配置没有通过 schema 校验: {errors}; entry={entry}"
    assert "接口" in contract.render(candidates) or "|" in contract.render(candidates)


def test_real_fetcher_reads_js_bundles(fixture_site, host):
    """回归：application/javascript 曾经不在 web.TEXTUAL 里，脚本拿到空 body，接口全丢。"""
    from crawler import endpoints
    from crawler.web import Fetcher

    home = Fetcher({"domain": host}).get_many([fixture_site + "/"])[0][2]
    scanned = endpoints.scan(home, fixture_site + "/", fetcher=Fetcher({"domain": host}), limit=6)
    assert scanned["scripts"] >= 2, "内联 + 外链脚本都应参与扫描"
    kinds = {c["kind"] for c in scanned["candidates"]}
    assert {"next_data", "graphql"} <= kinds, f"外链 bundle 里的接口没读到: {kinds}"


# ---------- 2. 抓包 → 分页与字段推理 ----------

def _har_entry(url, text):
    return {"request": {"method": "GET", "url": url, "headers": []},
            "response": {"status": 200,
                         "headers": [{"name": "content-type", "value": "application/json"}],
                         "content": {"mimeType": "application/json", "text": text}}}


def test_har_infers_pagination_and_fields(fixture_site):
    from crawler import contract

    har = {"log": {"version": "1.2", "entries": [
        _har_entry(fixture_site + "/api/v2/articles?page=1", json.dumps(_articles(1))),
        _har_entry(fixture_site + "/api/v2/articles?page=2", json.dumps(_articles(2))),
        _har_entry(fixture_site + "/static/app.js", APP_JS),
    ]}}
    candidates = contract.from_har(har, min_bytes=10)
    assert candidates, "抓包里应该至少有 /api/v2/articles 一个候选"
    top = candidates[0]
    assert top["pagination"]["param"] == "page"
    assert {str(v) for v in top["pagination"]["values"]} == {"1", "2"}
    assert {"data", "title"} & set(top["fields"]), f"字段推理没抓到 keys: {top['fields']}"
    assert top["sample_count"] >= 2


def test_detect_attaches_endpoints_and_keeps_channel(monkeypatch, fixture_site):
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE", "1")
    monkeypatch.delenv("ENDPOINT_AUTOCONFIG", raising=False)
    from crawler.adhoc import detect

    auto = detect(fixture_site + "/", public=False)
    assert auto["channel"] == "html", f"探测结果不能因为挖到接口就换通道: {auto}"
    found = (auto.get("detected") or {}).get("endpoints") or []
    assert found, "detected.endpoints 应该是非空"
    assert any("/api/" in item.get("url", "") or "next_data" == item.get("kind") for item in found)


# ---------- 6. 预览默认行为不变 + 候选契约可选 ----------

def test_preview_default_unchanged_and_contract_opt_in(monkeypatch, fixture_site):
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE", "1")
    import web_preview

    result = web_preview.preview(fixture_site + "/", {})
    for key in ("url", "channel", "transport", "samples", "issues", "editable", "rules"):
        assert key in result, f"预览返回结构缺 {key}"

    params = inspect.signature(web_preview.preview).parameters
    if "capture_contract" in params:
        # 验收不启动真浏览器（会留下 playwright 的全局事件循环，污染后续用例）：
        # 把 capture 判定成不可用，让它走"首页 HTML 挖接口"这条降级路。
        from crawler import capture
        monkeypatch.setattr(capture, "available", lambda: False)
        richer = web_preview.preview(fixture_site + "/", {}, capture_contract=True)
        assert isinstance(richer.get("candidates"), list)
        assert richer.get("capture_note") in ("har", "html", "unavailable", None)


@pytest.fixture
def preview_http(monkeypatch, fixture_site):
    from crawler import capture
    from serve import H

    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE", "1")
    monkeypatch.delenv("CRAWL_WEB_PASSWORD", raising=False)
    monkeypatch.setattr(capture, "available", lambda: False)
    with ThreadingHTTPServer(("127.0.0.1", 0), H) as server:
        server.desktop = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def post(path, body):
                return requests.post(f"http://127.0.0.1:{server.server_port}{path}", json=body, timeout=25)
            yield post, {"url": fixture_site + "/", "channel": "html"}
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("options", [{}, {"capture_contract": False}, {"capture_contract": "true"}])
def test_preview_http_keeps_legacy_keys_without_boolean_true(preview_http, options):
    post, site = preview_http
    response = post("/api/preview", dict(site, **options))
    assert response.status_code == 200
    assert set(response.json()) == {"url", "channel", "transport", "source", "samples", "issues",
                                    "editable", "rules", "preview_id", "available_kinds", "selection"}


def test_preview_http_true_returns_script_candidates(preview_http):
    post, site = preview_http
    response = post("/api/preview", dict(site, capture_contract=True))
    assert response.status_code == 200
    result = response.json()
    assert result["capture_note"] == "html"
    assert any(row["path_template"] == "/api/v2/articles" for row in result["candidates"])


def test_preview_http_limits_same_site_candidates(preview_http, monkeypatch):
    import web_preview
    from crawler import contract

    post, site = preview_http
    paths = [{"kind": "api", "url": site["url"] + f"api/v2/list-{i}?page=1&access_token=fixture"}
             for i in range(20)]
    paths += [{"kind": "api", "url": "https://other.test/api/v2/list"},
              {"kind": "api", "url": site["url"] + "api/v2/write", "method": "POST"}]
    candidates = contract.from_endpoints(paths)
    monkeypatch.setattr(web_preview, "_html_endpoint_candidates",
                        lambda *_, **__: (candidates, True))
    response = post("/api/preview", dict(site, capture_contract=True))
    assert response.status_code == 200, response.text
    rows = response.json()["candidates"]
    assert len(rows) == 5
    expected = [row["endpoint"] for row in candidates
                if row["host"] == urlsplit(site["url"]).netloc and row["method"] == "GET"][:5]
    assert [row["endpoint"] for row in rows] == expected
    assert all(row["query"] == {"page": ["1"]} and "fixture" not in row["evidence"]["sample_url"]
               for row in rows)


def test_public_archive_preview_uses_snapshot_without_request_queue(preview_http, monkeypatch):
    import cdx_seed
    import web_preview
    from crawler.channels import archive

    post, site = preview_http
    story = site["url"] + "story/one"
    html = ("<html><article><h1>Local archive article</h1><p>" +
            "Residents share detailed information about education and community services. " * 12 +
            "</p></article></html>")
    monkeypatch.setattr(cdx_seed, "cdx_urls", lambda *_args, **_kwargs: [story])
    monkeypatch.setattr(web_preview.paywall, "archive_fetch",
                        lambda target, *, public: (200, html, "archive_org") if public and target == story else (0, "", ""))
    monkeypatch.setattr(archive, "RequestQueue", lambda *args: pytest.fail("Preview must not open a request queue"))
    preview = post("/api/preview", {"url": site["url"], "channel": "archive"})
    assert preview.status_code == 200
    result = preview.json()
    assert result["channel"] == result["transport"] == "archive"
    assert result["preview_id"] and result["samples"][0]["url"] == story


def test_contract_entry_http_exports_validated_yaml(preview_http):
    from crawler import contract, schema

    post, site = preview_http
    host = urlsplit(site["url"]).netloc
    candidate, = contract.from_endpoints([{
        "kind": "json-list", "url": site["url"] + "api/v2/articles?lang=fr&token=fixture",
    }])
    response = post("/api/contract/entry", {"domain": host, "candidate": candidate})
    assert response.status_code == 200
    data = response.json()
    assert data["errors"] == []
    assert data["warnings"]
    assert schema.validate_site(data["entry"])[0] == []
    assert yaml.safe_load(data["yaml"]) == [data["entry"]]
    assert data["entry"]["api_list"] == "/api/v2/articles?page={page}&lang=fr"


@pytest.mark.parametrize("candidate", [{}, {"host": "example.test", "path_template": "//other.test/api",
                                            "channel": "json-api"}])
def test_contract_entry_http_rejects_malformed_candidate(preview_http, candidate):
    post, _ = preview_http
    response = post("/api/contract/entry", {"candidate": candidate})
    assert response.status_code == 400
    assert response.json()["errors"] == [response.json()["error"]]
    assert "yaml" not in response.json()


def test_mine_site_cli_outputs_json_with_optional_har(fixture_site, tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE", "1")
    program = Path(__file__).resolve().parents[1] / "mine_site.py"
    command = [sys.executable, str(program), fixture_site + "/", "--json"]
    result = json.loads(subprocess.run(command, capture_output=True, text=True, check=True, timeout=30).stdout)
    assert set(result) == {"url", "notes", "candidates", "suggestions"}
    assert result["candidates"] and result["suggestions"]

    har_path = tmp_path / "fixture.har"
    har_path.write_text(json.dumps({"log": {"entries": [_har_entry(
        fixture_site + "/api/v2/articles?page=1", json.dumps(_articles()))]}}), encoding="utf-8")
    result = json.loads(subprocess.run(command + ["--har", str(har_path)],
                                       capture_output=True, text=True, check=True, timeout=30).stdout)
    assert any("HAR 候选 1 条" in note for note in result["notes"])


# ---------- 7. 体检项：引擎与抓包能力 ----------

def test_engine_and_capture_arms():
    from crawler import browser_playwright

    assert isinstance(browser_playwright.available(), bool)
    try:
        from crawler import capture
    except Exception:
        pytest.skip("capture 模块未接入")
    assert isinstance(capture.available(), bool)
