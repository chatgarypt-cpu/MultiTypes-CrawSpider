#!/usr/bin/env python3
"""Portable dashboard + durable crawl queue. See deploy/README.md."""
import argparse
import base64
import fcntl
import hmac
import json
import mimetypes
import os
from pathlib import Path
import signal
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote, urlsplit

from crawler.config import DATA, ROOT
import status as status_mod
import web_jobs

UI = Path(ROOT) / 'ui' / 'dist'
CFG = Path(DATA) / 'queue_config.json'
_STATUS_CACHE = {'at': 0, 'payload': ''}
STATUS_LOCK = threading.Lock()


def cached_status():
    with STATUS_LOCK:
        if time.time() - _STATUS_CACHE['at'] > 3:
            _STATUS_CACHE.update(at=time.time(), payload=json.dumps(status_mod.build_status(), ensure_ascii=False))
        return _STATUS_CACHE['payload']


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def send(self, code, body, mime='application/json; charset=utf-8', headers=None):
        if not isinstance(body, bytes):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        # do_POST 会置 close_connection=True；不显式声明的话客户端会把死连接放回池里，
        # 下一个请求就 RemoteDisconnected（requests/urllib3 只认 Connection: close 头）。
        if self.close_connection and not any(k.lower() == 'connection' for k in (headers or {})):
            self.send_header('Connection', 'close')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False))

    def authorized(self):
        password = os.environ.get('CRAWL_WEB_PASSWORD', '')
        if password:
            expected = 'Basic ' + base64.b64encode(('admin:' + password).encode()).decode()
            if not hmac.compare_digest(self.headers.get('Authorization', ''), expected):
                self.send(401, '需要登录', 'text/plain; charset=utf-8', {'WWW-Authenticate': 'Basic realm="Crawler", charset="UTF-8"'})
                return False
        return True

    def file(self, root, relative, download=False):
        root = root.resolve()
        path = (root / unquote(relative)).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return self.json({'error': '文件不存在'}, 404)
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if path.suffix == '.log':
            mime = 'text/plain; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(path.stat().st_size))
        self.send_header('X-Content-Type-Options', 'nosniff')
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="' + path.name.replace('"', '') + '"')
        self.end_headers()
        with path.open('rb') as f:
            import shutil
            shutil.copyfileobj(f, self.wfile)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/healthz':
            return self.json({'ok': True, 'ui_ready': (UI / 'index.html').is_file()})
        if not self.authorized():
            return
        if path in ('/', '/ui', '/crawl', '/index.html', '/dashboard.html'):
            return self.send(302, '', headers={'Location': '/ui/'})
        if path == '/api/config':
            return self.json(web_jobs.read(CFG, {'concurrency': 4}))
        if path == '/api/jobs':
            with web_jobs.LOCK:
                return self.json({'jobs': web_jobs.jobs()})
        if path == '/api/stream':
            return self.stream()
        if path.startswith('/api/jobs/'):
            bits = path.split('/')
            try:
                directory = web_jobs.job_dir(bits[3])
                if len(bits) == 5 and bits[4] == 'log':
                    return self.file(directory, 'run.log')
                if len(bits) == 6 and bits[4] == 'files':
                    return self.file(directory / 'data' / 'csv', bits[5], True)
            except ValueError:
                pass
        if path.startswith('/ui/'):
            return self.file(UI, path[4:] or 'index.html')
        if path.startswith('/csv/'):
            return self.file(Path(DATA) / 'csv', path[5:], True)
        return self.json({'error': '页面不存在'}, 404)

    def stream(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        try:
            while not self.server.stop.is_set():
                self.wfile.write(f'data: {cached_status()}\n\n'.encode())
                self.wfile.flush()
                self.server.stop.wait(2)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        self.close_connection = True  # rejected bodies must not poison a keep-alive connection
        if not self.authorized():
            return
        # JSON-only and same-origin requests prevent cross-site form submissions.
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            return self.json({'error': '不允许跨站提交'}, 403)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.json({'error': '需要 JSON 请求'}, 415)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length < 0 or length > 1024 * 1024:
                self.close_connection = True
                return self.json({'error': '请求正文不能超过 1 MB，请分次提交'}, 413)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('请求格式不正确')
            if self.path == '/api/concurrency':
                n = body.get('n')
                if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                    raise ValueError('并发数必须为正整数')
                web_jobs.write(CFG, {'concurrency': n})
                return self.json({'ok': True, 'concurrency': n})
            if self.path == '/api/preview':
                from web_preview import preview
                return self.json(preview(body.get('url'), body.get('rules', {}),
                                         body.get('channel', 'auto'), body.get('item_selector', ''),
                                         body.get('scope')))
            if self.path == '/api/crawl':
                if 'preview_id' in body:
                    from web_preview import confirm
                    return self.json({'ok': True, 'results': confirm(body['preview_id'])}, 202)
                values = body.get('urls', [body.get('url', '')])
                return self.json({'ok': True, 'results': web_jobs.submit(values)}, 202)
            if self.path.startswith('/api/jobs/'):
                parts = self.path.split('/')
                if len(parts) == 5 and parts[4] == 'resume':
                    web_jobs.resume(parts[3])
                    return self.json({'ok': True})
                if len(parts) == 5 and parts[4] == 'stop':
                    with web_jobs.LOCK:
                        directory = web_jobs.job_dir(parts[3])
                        job = web_jobs.read(directory / 'job.json')
                        if job.get('status') == 'queued':
                            job.update(status='interrupted', stage='已停止')
                            web_jobs.write(directory / 'job.json', job)
                        elif web_jobs.held(directory / 'worker.lock'):
                            os.kill(int((directory / 'worker.pid').read_text()), signal.SIGTERM)
                        else:
                            raise ValueError('任务不在运行，请稍后刷新')
                    return self.json({'ok': True})
            return self.json({'error': '接口不存在'}, 404)
        except (ValueError, OSError, TypeError) as exc:
            return self.json({'error': str(exc)}, 400)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=os.getenv('CRAWL_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('CRAWL_PORT', '8787')))
    args = parser.parse_args()
    if args.host not in ('localhost', '127.0.0.1', '::1') and not os.getenv('CRAWL_WEB_PASSWORD'):
        parser.error('对外监听必须设置 CRAWL_WEB_PASSWORD；登录用户名为 admin')
    if not (UI / 'index.html').exists():
        parser.error('前端尚未构建，请先运行 deploy/start-web（或 npm --prefix collector/ui run build）')
    Path(DATA).mkdir(parents=True, exist_ok=True)
    lock = open(Path(DATA) / 'web-server.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('此数据目录已有网页服务运行，请使用现有服务或选择另一数据目录')
    server = ThreadingHTTPServer((args.host, args.port), H)
    server.daemon_threads = True
    server.stop = threading.Event()
    threading.Thread(target=web_jobs.scheduler, args=(server.stop,), daemon=True).start()
    from verify_coverage import refresh_registered
    threading.Thread(target=refresh_registered, args=(server.stop,), daemon=True).start()
    print(f'采集页面：http://{args.host}:{server.server_port}/  数据目录：{DATA}', flush=True)
    def shutdown(*_):
        server.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        server.stop.set()
        server.server_close()
        lock.close()


if __name__ == '__main__':
    main()
