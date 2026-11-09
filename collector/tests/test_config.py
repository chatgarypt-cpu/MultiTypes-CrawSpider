"""sources.yaml 校验（重复 key / 发现入口 / 正则）与 check-site 干跑测试。

全部离线：check-site 用 127.0.0.1 的本地 http.server；校验用例用内联 YAML 字符串。
"""
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import collect
from crawler import schema, store, web

DUP_TEXT = """\
- domain: a.test
  channel: html
  base: https://a.test
  list_urls:
    - /x
  list_urls:
    - /y
"""

CLEAN_TEXT = """\
- domain: a.test
  channel: html
  base: https://a.test
  list_urls:
    - /
  link_pattern: /news/
"""

LIST_HTML = """<html><body>
<a href="/news/one">one</a>
<a href="/news/two">two</a>
<a href="/news/three">three</a>
<a href="/about">about</a>
</body></html>"""


def make_handler(routes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            item = routes.get(path) or routes.get("*")
            if item is None:
                self.send_response(404)
                self.end_headers()
                return
            status, ctype, body = item
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


@pytest.fixture
def serve():
    servers = []

    def _serve(routes):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(routes))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}"

    yield _serve
    for srv in servers:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def offline_politeness(monkeypatch):
    """check-site 的发现阶段会查 robots/限速：这里用 no-op 版本（离线且不写 data/）。"""
    class Robots:
        def allowed(self, url):
            return True

        def crawl_delay(self, url):
            return None

    class Limiter:
        def acquire(self, url, timeout=30, min_interval=None):
            pass

        def penalize(self, url):
            pass

        def reward(self, url):
            pass

    monkeypatch.setattr(web, "ROBOTS", Robots())
    monkeypatch.setattr(web, "LIMITER", Limiter())


# ---------- 重复 key 检测 ----------
def test_find_duplicate_keys_clean():
    assert schema.find_duplicate_keys(CLEAN_TEXT, "sources.yaml") == []


def test_find_duplicate_keys_reported():
    errors = schema.find_duplicate_keys(DUP_TEXT, "sources.yaml")
    assert len(errors) == 1
    assert "duplicate key 'list_urls'" in errors[0]
    assert "at line 6" in errors[0] and "first at line 4" in errors[0]
    assert "a.test" in errors[0]                     # 已知所在站点，便于定位


def test_find_duplicate_keys_syntax_error_not_raised():
    errors = schema.find_duplicate_keys("- domain: [unclosed\n", "bad.yaml")
    assert len(errors) == 1
    assert "syntax error" in errors[0].lower()


def test_validate_file_duplicate_is_error(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(DUP_TEXT, encoding="utf-8")
    errors, warns, sites = schema.validate_file(path)
    assert any("duplicate key 'list_urls'" in e for e in errors)
    assert len(sites) == 1


def test_validate_file_clean_has_no_errors(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(CLEAN_TEXT, encoding="utf-8")
    errors, warns, sites = schema.validate_file(path)
    assert errors == []
    assert len(sites) == 1


# ---------- 发现入口 / 正则 mismatch ----------
def test_html_missing_discovery_entry_is_error():
    """html 无 list_url/list_urls 且未 disabled = 配置错误，不再只是 warning。"""
    errors, warns = schema.validate_site({"domain": "h.test", "channel": "html", "base": "https://h.test"})
    assert any("list_url/list_urls" in e for e in errors)
    assert not any("list_url/list_urls" in w for w in warns)


def test_html_missing_entry_but_disabled_is_classified():
    """不可采的 html 站显式 disabled 后：0 error，warning 是分类说明而非"缺入口"。"""
    site = {"domain": "h.test", "channel": "html", "base": "https://h.test",
            "disabled": True, "disabled_reason": "站点已下线"}
    errors, warns = schema.validate_site(site)
    assert errors == []
    assert any("disabled site (缺列表入口)" in w and "站点已下线" in w for w in warns)
    assert not any("discovery entry missing" in w for w in warns)


def test_disabled_with_list_entry_classified_as_unreachable():
    site = {"domain": "h.test", "channel": "html", "base": "https://h.test",
            "list_urls": ["/"], "disabled": True, "disabled_reason": "403 反爬"}
    errors, warns = schema.validate_site(site)
    assert errors == []
    assert any("disabled site (不可访问/不采)" in w for w in warns)


def test_disabled_keys_are_known_not_typos():
    site = {"domain": "h.test", "channel": "html", "base": "https://h.test",
            "list_urls": ["/"], "disabled": True, "disabled_reason": "已下线"}
    errors, warns = schema.validate_site(site)
    assert errors == []
    assert not any("unknown key" in w for w in warns)


def test_disabled_without_reason_still_classified():
    site = {"domain": "h.test", "channel": "html", "base": "https://h.test", "disabled": True}
    errors, warns = schema.validate_site(site)
    assert any("disabled site" in w and "未写原因" in w for w in warns)


def test_disabled_reason_without_disabled_warns():
    site = {"domain": "h.test", "channel": "html", "base": "https://h.test",
            "list_urls": ["/"], "disabled_reason": "忘了写 disabled"}
    errors, warns = schema.validate_site(site)
    assert errors == []
    assert any("disabled_reason is set but disabled is not true" in w for w in warns)


def test_disabled_wrong_type_is_error():
    site = {"domain": "h.test", "channel": "html", "base": "https://h.test",
            "list_urls": ["/"], "disabled": "true"}
    errors, warns = schema.validate_site(site)
    assert any("disabled must be bool" in e for e in errors)


def test_sitemap_missing_discovery_entry_is_error_and_warning():
    errors, warns = schema.validate_site({"domain": "s.test", "channel": "sitemap", "base": "https://s.test"})
    assert any("requires 'sitemap'" in e for e in errors)
    assert any("discovery entry missing" in w for w in warns)


def test_invalid_regex_warns():
    errors, warns = schema.validate_site({"domain": "r.test", "channel": "html", "base": "https://r.test",
                                          "list_url": "/", "link_pattern": "/news/["})
    assert errors == []
    assert any("not a valid regex" in w for w in warns)


def test_validate_all_accepts_valid_sites():
    sites = [
        {"domain": "a.test", "channel": "html", "base": "https://a.test", "list_urls": ["/"], "link_pattern": "/news/"},
        {"domain": "b.test", "channel": "sitemap", "base": "https://b.test", "sitemap": "/sitemap.xml"},
    ]
    errors, warns = schema.validate_all(sites)
    assert errors == []


# ---------- check-site 干跑 ----------
def test_check_site_html_dry_run(serve, offline_politeness, monkeypatch):
    base = serve({"/": (200, "text/html", LIST_HTML)})
    monkeypatch.setattr(store, "save_cursor", lambda *a, **k: pytest.fail("check-site must not write cursor"))
    monkeypatch.setattr(store, "append_rows", lambda *a, **k: pytest.fail("check-site must not store rows"))
    monkeypatch.setattr(collect, "collect_site", lambda *a, **k: pytest.fail("check-site must not crawl bodies"))
    result = collect.check_site({"domain": "a.test", "channel": "html", "base": base, "list_urls": ["/"]})
    assert result["discovered"] == 3
    assert len(result["sample"]) == 3
    assert result["sample"][0].startswith(base)
    assert "about" not in " ".join(result["sample"])


def test_check_site_sitemap_dry_run(serve, offline_politeness):
    sitemap = ("<?xml version=\"1.0\"?><urlset>"
               "<url><loc>/news/1</loc></url>"
               "<url><loc>/news/2</loc></url>"
               "<url><loc>/news/3</loc></url></urlset>")
    base = serve({"/sitemap.xml": (200, "application/xml", sitemap)})
    result = collect.check_site({"domain": "s.test", "channel": "sitemap", "base": base, "sitemap": "/sitemap.xml"})
    assert result["discovered"] == 3
    assert result["sample"][0] == "/news/1"


def test_check_site_cli_prints(serve, offline_politeness, monkeypatch, tmp_path, capsys):
    base = serve({"/": (200, "text/html", LIST_HTML)})
    (tmp_path / "sources.yaml").write_text(
        f"- domain: a.test\n  channel: html\n  base: {base}\n  list_urls:\n    - /\n", encoding="utf-8")
    monkeypatch.setattr(collect, "ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["collect.py", "--check-site", "a.test"])
    collect.main()
    out = capsys.readouterr().out
    assert "discovered 3" in out
    assert "/news/one" in out
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sources.yaml"]   # 不写 cursor


def test_check_site_cli_unknown_domain(monkeypatch, tmp_path, capsys):
    (tmp_path / "sources.yaml").write_text(CLEAN_TEXT, encoding="utf-8")
    monkeypatch.setattr(collect, "ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["collect.py", "--check-site", "nope.test"])
    with pytest.raises(SystemExit) as exc:
        collect.main()
    assert "not found" in str(exc.value)


def test_cli_validate_reports_duplicate_key(monkeypatch, tmp_path, capsys):
    (tmp_path / "sources.yaml").write_text(DUP_TEXT, encoding="utf-8")
    monkeypatch.setattr(collect, "ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["collect.py", "--validate"])
    with pytest.raises(SystemExit) as exc:
        collect.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "ERROR" in out and "duplicate key 'list_urls'" in out
