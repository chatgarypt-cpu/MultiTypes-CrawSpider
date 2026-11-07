#!/usr/bin/env python3
"""Static dashboard + control endpoints (concurrency, ad-hoc crawl)."""
import json, os, shutil, subprocess, sys, time, uuid
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import status as status_mod

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(ROOT, "data", "queue_config.json")
JOBS = os.path.join(ROOT, "data", "jobs.json")
PROCS = {}                                   # job_id -> Popen, so finished children get reaped
LOGS = os.path.join(ROOT, "data", "jobs")
UI = os.path.join(ROOT, "ui", "dist")

_STATUS_CACHE = {"at": 0.0, "payload": ""}


def cached_status(ttl=3.0):
    """build_status() 要扫全部站点的 urls.txt，很贵；多客户端/高频 SSE 共用一份 3 秒缓存。"""
    now = time.time()
    if not _STATUS_CACHE["payload"] or now - _STATUS_CACHE["at"] >= ttl:
        _STATUS_CACHE["payload"] = json.dumps(status_mod.build_status(), ensure_ascii=False)
        _STATUS_CACHE["at"] = now
    return _STATUS_CACHE["payload"]

class H(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"     # SSE needs keep-alive semantics; HTTP/1.0 closes per response
    def __init__(self, *a, **kw): super().__init__(*a, directory=ROOT, **kw)
    def _html(self, text):
        b = text.encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def translate_path(self, path):
        clean = path.split("?")[0].split("#")[0]
        if clean.startswith("/ui/"):
            rel = clean[len("/ui/"):] or "index.html"
            return os.path.join(UI, rel)
        if clean.startswith("/csv/"):
            name = os.path.basename(clean[len("/csv/"):])
            return os.path.join(ROOT, "data", "csv", name)
        return super().translate_path(path)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/dashboard.html") or self.path == "/ui":
            self.send_response(302)
            self.send_header("Location", "/ui/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/api/config":
            return self._json(read_cfg())
        if self.path == "/api/jobs":
            return self.do_GET_jobs()
        if self.path == "/crawl":
            return self._html(CRAWL_PAGE)
        if self.path == "/api/stream":
            return self._stream()
        return super().do_GET()

    def _stream(self):
        """Server-Sent Events: push status (cached a few seconds) until the client goes away."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                payload = cached_status()
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError):
            return
    def do_GET_jobs(self):
        return self._json({"jobs": load_jobs()})

    def do_POST(self):
        if self.path == "/api/crawl":
            return self.start_crawl()
        if self.path != "/api/concurrency":
            return self.send_error(404)
        try:
            n = int(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["n"])
        except Exception:
            return self.send_error(400)
        os.makedirs(os.path.dirname(CFG), exist_ok=True)
        json.dump({"concurrency": max(1, min(10, n))}, open(CFG, "w"))
        self._json({"ok": True, **read_cfg()})
    # ---------- ad-hoc crawl ----------
    def start_crawl(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        except Exception:
            return self.send_error(400)
        url = (body.get("url") or "").strip()
        if not url:
            return self._json({"ok": False, "error": "url required"})
        mode = body.get("mode") or "site"          # site | article
        limit = int(body.get("limit") or 20)
        job_id = uuid.uuid4().hex[:8]
        os.makedirs(LOGS, exist_ok=True)
        log_path = os.path.join(LOGS, job_id + ".log")
        target = os.path.join(ROOT, "collect.py")
        args = ["--url", url] if mode == "article" else ["--site-url", url, "--limit", str(limit)]
        uv = shutil.which("uv")
        if uv:
            cmd = [uv, "run", "--quiet", "--with", "requests", "--with", "pyyaml",
                   "--with", "trafilatura", "--with", "curl_cffi", "python3", target] + args
        else:
            cmd = [sys.executable, target] + args
        env = dict(os.environ, PYTHONPATH=ROOT)
        with open(log_path, "w") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT, env=env)
        PROCS[job_id] = proc
        jobs = load_jobs()
        jobs[job_id] = {"id": job_id, "url": url, "mode": mode, "limit": limit, "pid": proc.pid,
                        "status": "running", "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "log": os.path.relpath(log_path, ROOT)}
        save_jobs(jobs)
        self._json({"ok": True, "job": jobs[job_id]})

    def log_message(self, *a): pass

def load_jobs():
    try:
        jobs = json.load(open(JOBS))
    except Exception:
        jobs = {}
    for j in jobs.values():                     # reconcile with real process state
        if j.get("status") == "running":
            proc = PROCS.get(j["id"])
            try:
                if proc is not None:
                    code = proc.poll()
                    if code is not None:
                        j["status"] = "finished" if code == 0 else "failed"
                        j.setdefault("finished", time.strftime("%Y-%m-%dT%H:%M:%S"))
                else:
                    os.kill(j["pid"], 0)         # not our child (server restarted): best effort
            except OSError:
                j["status"] = "finished"
                j.setdefault("finished", time.strftime("%Y-%m-%dT%H:%M:%S"))
            except Exception:
                pass
    return jobs


def save_jobs(jobs):
    os.makedirs(os.path.dirname(JOBS), exist_ok=True)
    json.dump(jobs, open(JOBS, "w"), ensure_ascii=False, indent=1)


def read_cfg():
    try: return json.load(open(CFG))
    except Exception: return {"concurrency": 4}




CRAWL_PAGE = """<!doctype html><meta charset="utf-8"><title>启动采集</title>
<style>body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:760px;margin:48px auto;padding:0 20px;color:#1d1d1f}
h1{font-size:24px;letter-spacing:-.02em}input,select,button{font:inherit;padding:8px 10px;border:1px solid #d2d2d7;border-radius:8px}
input{width:100%;box-sizing:border-box}button{background:#0071e3;color:#fff;border:0;padding:9px 16px;border-radius:980px;cursor:pointer}
.row{display:flex;gap:10px;margin:12px 0}table{width:100%;border-collapse:collapse;margin-top:18px;font-size:13px}
td,th{text-align:left;padding:7px 8px;border-bottom:1px solid #f0f0f2}pre{background:#f5f5f7;padding:10px;border-radius:8px;overflow:auto;font-size:12px}
</style>
<h1>启动采集</h1>
<div class="row"><input id="url" placeholder="https://example.com 或 https://example.com/article"></div>
<div class="row">
  <select id="mode"><option value="site">整站（自动识别 sitemap/RSS/API）</option><option value="article">单篇文章</option></select>
  <input id="limit" type="number" value="20" style="width:90px" title="整站抓取条数上限">
  <button onclick="start()">开始</button>
</div>
<table id="jobs"><tr><th>任务</th><th>URL</th><th>状态</th><th>开始</th><th>日志</th></tr></table>
<script>
async function start(){
  const url=document.getElementById('url').value.trim();
  if(!url){alert('请输入网址');return;}
  const r=await fetch('/api/crawl',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url,mode:document.getElementById('mode').value,limit:+document.getElementById('limit').value})});
  const j=await r.json(); if(!j.ok) alert(j.error||'失败'); refresh();
}
async function refresh(){
  const d=await (await fetch('/api/jobs')).json();
  const t=document.getElementById('jobs');
  t.innerHTML='<tr><th>任务</th><th>URL</th><th>状态</th><th>开始</th><th>日志</th></tr>';
  Object.values(d.jobs).sort((a,b)=>b.started.localeCompare(a.started)).forEach(j=>{
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${j.id}</td><td>${j.url}</td><td>${j.status}</td><td>${j.started}</td>
      <td><a href="/data/${j.log}" target="_blank">log</a></td>`;
    t.appendChild(tr);
  });
}
refresh(); setInterval(refresh, 5000);
</script>"""


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8787), H).serve_forever()
