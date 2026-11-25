"""试采遇到 robots 限制时返回可读诊断。"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import web_preview

ARTICLE = ("<html><body><article><h1>Story one</h1><time>2026-11-01</time>"
           + "<p>" + ("Residents describe the harbour cleanup in detail. " * 12) + "</p>"
           + "</article></body></html>")
HOME = ('<html><body><h1>Fixture</h1><a href="/news/2026/11/story-one">Story one</a></body></html>')


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
        if self.path.startswith("/news/") or self.path.startswith("/story/"):
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
def _allow_private(monkeypatch):
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE", "1")


def test_robots_block_is_structured_not_a_dead_end(blocked_site):
    result = web_preview.preview(blocked_site + "/", {})
    assert result["samples"] == []
    issue = result["issues"][0]
    assert issue["reason"] == "robots_blocked"
    assert "robots" in issue["detail"]
    assert "公开存档" in issue["hint"]
    assert result.get("robots_override") is None
