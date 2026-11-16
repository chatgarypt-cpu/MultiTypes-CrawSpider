"""Real HTTP + subprocess acceptance tests; never use the production data directory."""
import json
import csv as csv_module
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]


class Source(BaseHTTPRequestHandler):
    mode = 'wp'
    def log_message(self, *args):
        pass
    def do_GET(self):
        p = urlsplit(self.path)
        query = parse_qs(p.query)
        path = query.get('rest_route', [p.path])[0]
        base = f'http://127.0.0.1:{self.server.server_port}'
        status, headers = 200, {}
        if self.mode == 'wp' and path == '/wp-json/wp/v2/types':
            body = json.dumps({'post': {'rest_base': 'posts'}, 'page': {'rest_base': 'pages'}, 'attachment': {'rest_base': 'media'}, 'reports': {'rest_base': 'reports'}})
        elif self.mode == 'wp' and path.startswith('/wp-json/wp/v2/'):
            kind = path.rsplit('/', 1)[-1]
            total = {'posts': 63, 'pages': 3, 'media': 0, 'reports': 2}.get(kind, 0)
            page = int(query.get('page', ['1'])[0]); per = int(query.get('per_page', ['50'])[0])
            headers = {'X-WP-Total': str(total), 'X-WP-TotalPages': str((total + per - 1) // per)}
            rows = [{'id': i, 'link': f'{base}/{kind}/{i}', 'title': {'rendered': f'Report {i}'},
                     'content': {'rendered': '<p>A public community news report with evidence and useful information.</p>' * 8},
                     'date': '2026-01-01T00:00:00'} for i in range((page-1)*per+1, min(page*per, total)+1)]
            body = json.dumps(rows)
        elif path == '/robots.txt':
            body = 'User-agent: *\nAllow: /'
        elif path == '/' or path.startswith('/story/'):
            i = int(path.rsplit('/', 1)[-1]) if path != '/' else 0
            if self.mode == 'slow':
                time.sleep(.2)
            body = f'<html><title>Community report {i}</title><article><h1>Report {i}</h1>' + '<p>Residents share detailed information about education and local community services.</p>' * 6 + '</article>'
            if i < 26:
                body += f'<a href="/story/{i+1}">Next article</a><a href="{base}/#top">Home</a>'
            body += '<a href="https://outside.invalid/news">Outside</a><a href="/logo.png">Image</a></html>'
        else:
            status, body = 404, 'Not found'
        raw = body.encode()
        self.send_response(status)
        for k, v in headers.items(): self.send_header(k, v)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers(); self.wfile.write(raw)


@pytest.fixture
def source():
    servers = []
    def start(mode='wp'):
        handler = type('FixtureSource', (Source,), {'mode': mode})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f'http://127.0.0.1:{server.server_port}'
    yield start
    for server in servers:
        server.shutdown(); server.server_close()


def wait_for(fn, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = fn()
        if result: return result
        time.sleep(.1)
    raise AssertionError('Timed out waiting for expected state')


@pytest.fixture
def service(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    env = dict(os.environ, CRAWL_DATA_DIR=str(tmp_path), CRAWL_ALLOW_PRIVATE='1',
               CRAWL_WEB_PASSWORD='test-password', WP_EMPTY_RETRIES='0', RETRY_BACKOFF='0')
    env.pop('CRAWL_HOST', None)
    procs = []
    session = requests.Session(); session.auth = ('admin', 'test-password')
    def start():
        proc = subprocess.Popen([sys.executable, str(ROOT/'collector/serve.py'), '--port', str(port)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        procs.append(proc)
        def ready():
            if proc.poll() is not None:
                raise AssertionError(proc.stdout.read().decode())
            try: return session.get(url+'/healthz', timeout=.5).ok
            except requests.RequestException: return False
        wait_for(ready)
        return proc
    proc = start()
    yield url, session, tmp_path, start, proc
    # Stop fixture workers before removing their temporary data.
    for pidfile in tmp_path.glob('web-jobs/*/worker.pid'):
        try: os.kill(int(pidfile.read_text()), 15)
        except ProcessLookupError: pass
    for p in procs:
        if p.poll() is None: p.terminate(); p.wait(timeout=8)


def finished(session, url, count):
    jobs = session.get(url+'/api/jobs').json()['jobs']
    return jobs if len(jobs) == count and all(j['status'] not in ('queued', 'running') for j in jobs) else False


def test_batch_full_crawl_download_and_restart(service, source):
    url, s, directory, start, proc = service
    wp, html = source('wp'), source('html')
    response = s.post(url+'/api/crawl', json={'urls': [wp, wp+'/', html, 'file:///etc/passwd']})
    assert response.status_code == 202
    assert [r['status'] for r in response.json()['results']] == ['queued', 'duplicate', 'queued', 'invalid']
    jobs = wait_for(lambda: finished(s, url, 2))
    assert all(j['status'] == 'finished' for j in jobs), jobs
    w = next(j for j in jobs if j['url'].startswith(wp))
    h = next(j for j in jobs if j['url'].startswith(html))
    assert w['saved'] == 68, w
    assert h['saved'] == 27, h
    assert len(w['files']) == 3  # empty media collection has no CSV
    csv = s.get(url + next(f['url'] for f in w['files'] if '.pages.' in f['name']))
    assert csv.status_code == 200 and 'attachment' in csv.headers['Content-Disposition']
    assert len(list(csv_module.DictReader(io.StringIO(csv.content.decode('utf-8-sig'))))) == 3
    assert s.get(url+w['log_url']).status_code == 200
    proc.terminate(); proc.wait(timeout=8); start()
    readback = s.get(url+'/api/jobs').json()['jobs']
    assert {j['id']: j['status'] for j in readback} == {j['id']: 'finished' for j in jobs}


def test_auth_validation_static_and_concurrency(service):
    url, s, directory, _, _ = service
    assert requests.get(url+'/api/jobs').status_code == 401
    for path in ('/sources.yaml', '/data/state.db', '/ui/%2e%2e/%2e%2e/sources.yaml', '/csv/%2e%2e/state.db'):
        assert s.get(url+path).status_code == 404
    assert s.post(url+'/api/crawl', json={'urls': 'bad'}).status_code == 400
    assert s.post(url+'/api/concurrency', json={'n': 0}).status_code == 400
    assert s.post(url+'/api/concurrency', json={'n': 15}).json()['concurrency'] == 15
    assert s.post(url+'/api/crawl', json={'urls': ['x.test']}, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert s.get(url+'/').status_code == 200


def test_stop_resume_preserves_rows(service, source):
    url, s, directory, _, _ = service
    target = source('slow')
    j = s.post(url+'/api/crawl', json={'url': target}).json()['results'][0]
    def progressed():
        item = s.get(url+'/api/jobs').json()['jobs'][0]
        return item if item['saved'] > 2 and item['status'] == 'running' else False
    wait_for(progressed)
    assert s.post(url+f'/api/jobs/{j["id"]}/stop', json={}).ok
    stopped = wait_for(lambda: finished(s, url, 1))[0]
    assert stopped['status'] == 'interrupted'
    assert s.post(url+f'/api/jobs/{j["id"]}/resume', json={}).ok
    end = wait_for(lambda: finished(s, url, 1))[0]
    assert end['status'] == 'finished' and end['saved'] == 27, end
    rows = list((directory/'web-jobs'/j['id']/'data').glob('*/articles.jsonl'))[0].read_text().splitlines()
    assert len(rows) == 27 and len({json.loads(r)['url'] for r in rows}) == 27


def test_no_receipt_is_interrupted_not_success(service):
    url, s, directory, _, _ = service
    job_id = 'a'*32
    d = directory/'web-jobs'/job_id; d.mkdir(parents=True)
    (d/'job.json').write_text(json.dumps({'id': job_id, 'url':'https://example.com/', 'status':'running', 'created': 1, 'started_epoch': 1}))
    assert s.get(url+'/api/jobs').json()['jobs'][0]['status'] == 'interrupted'


def test_concurrency_queues_without_dropping_batch(service, source):
    url, s, _, _, _ = service
    s.post(url+'/api/concurrency', json={'n': 1}).raise_for_status()
    a, b = source('slow'), source('slow')
    s.post(url+'/api/crawl', json={'urls': [a, b]}).raise_for_status()
    def scheduled():
        entries = s.get(url+'/api/jobs').json()['jobs']
        return entries if sorted(j['status'] for j in entries) == ['queued', 'running'] else False
    entries = wait_for(scheduled)
    assert len(entries) == 2
    queued = next(j for j in entries if j['status'] == 'queued')
    assert s.post(url+f'/api/jobs/{queued["id"]}/stop', json={}).ok
    assert next(j for j in s.get(url+'/api/jobs').json()['jobs'] if j['id'] == queued['id'])['status'] == 'interrupted'


def test_cli_zero_means_unlimited_and_preserves_cursor(monkeypatch, tmp_path):
    import collect
    from crawler import adhoc
    sentinel = tmp_path/'site.test'/'pages.cursor.json'
    sentinel.parent.mkdir(); sentinel.write_text('{"saved": 17}')
    site = {'domain': 'site.test', 'base': 'https://site.test', 'channel': 'wp-api'}
    calls = []
    monkeypatch.setattr(adhoc, 'detect', lambda url: site)
    monkeypatch.setattr(adhoc, 'remember', lambda site: None)
    monkeypatch.setattr(collect, 'collect_site', lambda s, limit, kind: calls.append((limit, kind)) or {'status': 'complete'})
    monkeypatch.setattr(collect, 'DATA', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['collect.py', '--site-url', 'site.test', '--limit', '0'])
    collect.main()
    assert calls == [(0, 'posts')]
    assert sentinel.read_text() == '{"saved": 17}'


def test_public_domain_fake_ip_and_literal_private_rejection(monkeypatch):
    import web_jobs
    monkeypatch.delenv('CRAWL_ALLOW_PRIVATE', raising=False)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.5',443))])
    assert web_jobs.normalize_url('example.com') == 'https://example.com/'
    with pytest.raises(ValueError): web_jobs.normalize_url('http://198.18.0.5/')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1',443))])
    with pytest.raises(ValueError): web_jobs.normalize_url('http://internal.test/')
