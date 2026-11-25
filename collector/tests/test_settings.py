"""设置页后端：robots 闸门与全局暂停。

验收点：设置里关掉 robots 后，**试采/探测/采集三处一起放行**（不再各有一套开关）。
全部离线：127.0.0.1 夹具站 + 全站 Disallow 的 robots.txt。
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from crawler import settings

ARTICLE = ("<html><body><article><h1>Story one</h1>"
           + "<p>" + ("Harbour cleanup details for residents. " * 12) + "</p></article></body></html>")
HOME = '<html><body><h1>F</h1><a href="/news/2026/11/story-one">s</a></body></html>'


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="text/html; charset=utf-8"):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/robots.txt":
            return self._send(200, "User-agent: *\nDisallow: /\n", "text/plain")
        if self.path.startswith("/news/"):
            return self._send(200, ARTICLE)
        return self._send(200, HOME)


@pytest.fixture(scope="module")
def blocked_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_env):
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE", "1")
    monkeypatch.delenv("CRAWL_BYPASS", raising=False)
    # CRAWL_BYPASS=0 由 conftest 设置；这里显式清掉哨兵文件，避免工作区文件影响判断
    (tmp_env / "CRAWL_BYPASS").unlink(missing_ok=True)
    yield


def test_default_respects_robots(blocked_site):
    import web_preview
    assert settings.respect_robots() is True
    result = web_preview.preview(blocked_site + "/", {})
    assert result["issues"][0]["reason"] == "robots_blocked"


def test_setting_off_lets_preview_through(blocked_site):
    """设置里关掉 robots 后，试采不需要任何单次豁免参数。"""
    import web_preview
    settings.write({"respect_robots": False})
    assert settings.respect_robots() is False
    assert settings.allow_target() is None
    result = web_preview.preview(blocked_site + "/", {})
    assert result["samples"], result["issues"]
    assert result["issues"] == [] or all(i["reason"] != "robots_blocked" for i in result["issues"])


def test_fetcher_follows_setting(blocked_site):
    from crawler.web import Fetcher
    host = blocked_site.split("//", 1)[1]
    strict = Fetcher({"domain": host})
    assert strict.robots is True
    _url, status, _body = strict.get_many([blocked_site + "/news/2026/11/story-one"])[0]
    assert status == -1, "默认应被 robots 拦下"

    settings.write({"respect_robots": False})
    relaxed = Fetcher({"domain": host})
    assert relaxed.robots is False
    _url, status, body = relaxed.get_many([blocked_site + "/news/2026/11/story-one"])[0]
    assert status == 200 and "Harbour cleanup" in body


def test_bypass_file_still_wins(blocked_site, tmp_env, monkeypatch):
    """运维哨兵文件优先级最高（不写设置也放行）。"""
    monkeypatch.delenv("CRAWL_BYPASS", raising=False)
    settings.write({"respect_robots": True})
    (tmp_env / "CRAWL_BYPASS").write_text("1\n")
    assert settings.respect_robots() is False


def test_pause_switch(tmp_env):
    assert settings.paused() is False
    assert settings.set_paused(True) is True
    assert (tmp_env / "CRAWL_PAUSED").exists()
    assert settings.set_paused(False) is False
    assert settings.status()["paused"] is False


def test_adhoc_worker_uses_server_settings(tmp_env, monkeypatch):
    settings.write({"respect_robots": False})
    settings.set_paused(True)
    monkeypatch.setenv("CRAWL_SETTINGS_DIR", str(tmp_env))
    monkeypatch.setattr(settings.config, "DATA", str(tmp_env / "web-jobs" / "fixture" / "data"))
    assert not settings.respect_robots()
    assert settings.paused()


def test_pause_preserves_queued_web_jobs(tmp_env, monkeypatch):
    import web_jobs
    monkeypatch.setattr(web_jobs, 'DATA', str(tmp_env))
    monkeypatch.setattr(web_jobs, 'JOBS', tmp_env / 'web-jobs')
    queued = web_jobs.submit(["http://127.0.0.1/"])[0]
    assert queued["status"] == "queued"
    settings.set_paused(True)
    monkeypatch.setattr(web_jobs.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("Paused queue launched a worker"))
    web_jobs.tick()
    assert web_jobs.jobs()[0]["status"] == "queued"


def test_settings_api_roundtrip(monkeypatch):
    import requests
    from serve import H

    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE", "1")
    monkeypatch.delenv("CRAWL_WEB_PASSWORD", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    server.desktop = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert requests.get(base + "/api/settings", timeout=10).json()["respect_robots"] is True
        wrote = requests.post(base + "/api/settings", json={"respect_robots": False}, timeout=10).json()
        assert wrote["respect_robots"] is False and settings.respect_robots() is False
        assert requests.post(base + "/api/settings", json={"respect_robots": "no"},
                             timeout=10).status_code == 400
        paused = requests.post(base + "/api/settings", json={"paused": True}, timeout=10).json()
        assert paused["paused"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
