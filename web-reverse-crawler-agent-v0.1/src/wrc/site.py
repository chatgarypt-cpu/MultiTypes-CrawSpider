import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ARTICLES = [
    {"id": str(i), "title": f"公开新闻示例 {i}", "url": f"http://127.0.0.1:8765/article/{i}", "published_at": f"2026-10-{i:02d}"}
    for i in range(1, 13)
]
HTML = (Path(__file__).resolve().parents[2] / "site" / "index.html").read_text(encoding="utf-8")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            payload = HTML.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif parsed.path == "/api/articles":
            params = parse_qs(parsed.query)
            try:
                page = max(1, int(params.get("page", ["1"])[0]))
                limit = min(20, max(1, int(params.get("limit", ["5"])[0])))
            except ValueError:
                self.send_error(400, "Invalid pagination")
                return
            start = (page - 1) * limit
            payload = json.dumps({"articles": ARTICLES[start:start+limit], "page": page, "total": len(ARTICLES)}, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

def serve():
    print("Demo: http://127.0.0.1:8765", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
