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
        if self.mode in ('wp', 'wp-partial', 'wp-no-body', 'wp-with-media') and path == '/wp-json/wp/v2/types':
            body = json.dumps({'post': {'rest_base': 'posts'}, 'page': {'rest_base': 'pages'}, 'attachment': {'rest_base': 'media'}, 'reports': {'rest_base': 'reports'}})
        elif self.mode == 'wp-partial' and path == '/wp-json/wp/v2/pages':
            status, body = 404, '{"code":"rest_no_route"}'
        elif self.mode in ('wp', 'wp-partial', 'wp-no-body', 'wp-with-media') and path.startswith('/wp-json/wp/v2/'):
            kind = path.rsplit('/', 1)[-1]
            total = {'posts': 63, 'pages': 3, 'media': int(self.mode == 'wp-with-media'), 'reports': 2}.get(kind, 0)
            page = int(query.get('page', ['1'])[0]); per = int(query.get('per_page', ['50'])[0])
            headers = {'X-WP-Total': str(total), 'X-WP-TotalPages': str((total + per - 1) // per)}
            rows = [{'id': i, 'link': f'{base}/{kind}/{i}', 'title': {'rendered': f'Report {i}'},
                     'content': {'rendered': '' if self.mode == 'wp-no-body' else '<p>A public community news report with evidence and useful information.</p>' * 8},
                     'date': '2026-01-01T00:00:00'} for i in range((page-1)*per+1, min(page*per, total)+1)]
            if kind == 'media':
                for row in rows:
                    row.update(source_url=f'{base}/image-{row["id"]}.png', mime_type='image/png')
            body = json.dumps(rows)
        elif path == '/robots.txt':
            body = 'User-agent: *\nAllow: /'
        elif self.mode == 'wp-partial' and path == '/':
            body = '<html><title>Home</title><nav><a href="/section/">News</a></nav></html>'
        elif self.mode == 'wp-partial' and path == '/section/' and query.get('p') == ['2']:
            body = '<html><title>News 2</title><nav><a href="/about/">About us</a></nav></html>'
        elif self.mode == 'wp-partial' and path == '/section/':
            body = '<html><title>News</title><nav><a href="?p=2" rel="next">Next</a></nav></html>'
        elif self.mode == 'wp-partial' and path == '/about/':
            body = '<html><title>About us</title><main><h1>About us</h1><p>This public page describes the community news team.</p></main></html>'
        elif self.mode == 'js-index' and path == '/':
            body = ('<html><title>Public archive</title><main id="app"></main>'
                    '<script>document.querySelector("#app").innerHTML='
                    '\'<a href="/story/26">Community story</a>\';</script></html>')
        elif self.mode == 'feed-mix' and path == '/':
            body = ('<html><head><title>Community archive</title>'
                    '<link rel="alternate" type="application/rss+xml" href="/journal.xml">'
                    '</head><body class="home"><a href="/news/">News</a><p>'
                    + 'This is a long summary on the home page. ' * 45 + '</p></body></html>')
        elif self.mode == 'feed-mix' and path == '/journal.xml':
            body = ('<rss version="2.0"><channel><title>Community reports</title>'
                    '<item><link><![CDATA[/story/26]]></link></item>'
                    '<item><link>/story/25</link></item></channel></rss>')
        elif self.mode == 'feed-mix' and path == '/news/':
            body = '<html><title>News</title><body class="archive"><a rel="next" href="/news/page/2/">Next</a></body></html>'
        elif self.mode == 'feed-mix' and path == '/news/page/2/':
            body = '<html><title>Older news</title><body class="archive"><a href="/story/25">Old story</a></body></html>'
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
def service(tmp_path, request):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    env = dict(os.environ, CRAWL_DATA_DIR=str(tmp_path), CRAWL_ALLOW_PRIVATE='1',
               CRAWL_WEB_PASSWORD='test-password', WP_EMPTY_RETRIES='0', RETRY_BACKOFF='0')
    env.pop('CRAWL_HOST', None)
    procs = []
    session = requests.Session(); session.auth = ('admin', 'test-password')
    def start(root=ROOT):
        python = root / '.venv/bin/python' if root != ROOT else sys.executable
        proc = subprocess.Popen([str(python), str(root/'collector/serve.py'), '--port', str(port)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        procs.append(proc)
        def ready():
            if proc.poll() is not None:
                raise AssertionError(proc.stdout.read().decode())
            try: return session.get(url+'/healthz', timeout=.5).ok
            except requests.RequestException: return False
        wait_for(ready)
        return proc
    previous = os.environ.get('CRAWL_UPGRADE_FROM') if getattr(request, 'param', False) else None
    proc = start(Path(previous) if previous else ROOT)
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


def test_monitor_csv_download_refreshes_without_background_export(service):
    url, session, directory, _, _ = service
    site = directory / "monitor.test"
    site.mkdir()
    source = site / "articles.jsonl"
    source.write_text('{"url":"https://monitor.test/1"}\n')
    download = url + "/csv/monitor.test.csv"
    first = session.get(download)
    first.raise_for_status()
    assert "https://monitor.test/1" in first.text
    with source.open("a") as output:
        output.write('{"url":"https://monitor.test/2"}\n')
    second = session.get(download)
    second.raise_for_status()
    assert "https://monitor.test/2" in second.text
    assert session.get(url + "/api/quality").json()["sites"] == []


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


def test_failed_wp_page_collection_falls_back_to_public_navigation(service, source):
    url, session, directory, _, _ = service
    target = source('wp-partial')
    session.post(url+'/api/crawl', json={'url': target}).raise_for_status()
    job = wait_for(lambda: finished(session, url, 1))[0]
    assert job['status'] == 'partial', job
    assert job['results']['pages']['status'] == 'interrupted'
    assert job['results']['web_pages']['saved'] > 0
    data = directory/'web-jobs'/job['id']/'data'
    rows = [json.loads(line) for line in next(data.glob('*/articles.jsonl')).read_text().splitlines()]
    assert any(r['url'] == target+'/about/' and 'community news team' in r['text'] for r in rows)
    assert len(rows) == len({r['url'] for r in rows})


def test_public_browser_renders_link_missing_from_initial_html(service, source):
    playwright = pytest.importorskip('playwright.sync_api')
    from crawler.browser_playwright import _executable
    executable = _executable()
    if not executable:
        with playwright.sync_playwright() as pw:
            executable = pw.chromium.executable_path
    if not Path(executable).exists():
        pytest.skip('Playwright Chromium is not installed')
    url, session, directory, _, _ = service
    target = source('js-index')
    session.post(url+'/api/crawl', json={'url': target}).raise_for_status()
    job = wait_for(lambda: finished(session, url, 1))[0]
    data = directory/'web-jobs'/job['id']/'data'
    rows = [json.loads(line) for line in next(data.glob('*/articles.jsonl')).read_text().splitlines()]
    assert any(r['url'] == target+'/story/26' and r['text'] for r in rows), job


def test_declared_feed_and_archive_discover_distinct_details_once(service, source):
    url, session, directory, _, _ = service
    target = source('feed-mix')
    session.post(url+'/api/crawl', json={'url': target}).raise_for_status()
    job = wait_for(lambda: finished(session, url, 1))[0]
    assert job['channel'] == 'rss' and job['status'] == 'finished', job
    assert job['results']['posts']['skipped'] == {'discovery_page': 3, 'discovery_feed': 1}
    data = directory/'web-jobs'/job['id']/'data'
    rows = [json.loads(line) for line in next(data.glob('*/articles.jsonl')).read_text().splitlines()]
    assert sorted(r['url'] for r in rows) == [target+'/story/25', target+'/story/26']


@pytest.mark.parametrize("service", [True], indirect=True)
def test_stop_resume_preserves_rows(service, source):
    url, s, directory, start, proc = service
    target = source('slow')
    j = s.post(url+'/api/crawl', json={'url': target}).json()['results'][0]
    def progressed():
        item = s.get(url+'/api/jobs').json()['jobs'][0]
        return item if item['saved'] > 2 and item['status'] == 'running' else False
    wait_for(progressed)
    assert s.post(url+f'/api/jobs/{j["id"]}/stop', json={}).ok
    stopped = wait_for(lambda: finished(s, url, 1))[0]
    assert stopped['status'] == 'interrupted'
    download = url + stopped['files'][0]['url']
    before = s.get(download)
    before.raise_for_status()
    assert list(csv_module.DictReader(io.StringIO(before.content.decode('utf-8-sig'))))
    proc.terminate()
    proc.wait(timeout=8)
    start()
    assert s.get(download).content == before.content
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
    monkeypatch.setattr(collect, 'collect_site', lambda s, limit, kind, **kwargs: calls.append((limit, kind)) or {'status': 'complete'})
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


def test_preview_keeps_samples_out_of_storage_and_confirms_the_tested_rule(service, source):
    url, s, directory, _, _ = service
    article = source('wp') + '/story/1'
    selector = '//a[contains(@href, "/story/2")]/@href'
    invalid = s.post(url+'/api/preview', json={'url': article, 'rules': {'title': '//['}}).json()
    assert invalid['issues'][0]['reason'] == 'invalid_xpath'
    assert invalid['preview_id'] is None
    rule = 'concat(//h1, " | calibrated")'
    tested = s.post(url+'/api/preview', json={
        'url': article, 'rules': {'title': rule}, 'channel': 'html', 'item_selector': selector,
    }).json()
    assert tested['preview_id'], tested
    assert tested['samples'][0]['title'].endswith(' | calibrated')
    assert tested['samples'][0]['fields']['title']['matches'] == 1
    assert s.get(url+'/api/jobs').json()['jobs'] == []
    assert not list(directory.rglob('*.csv'))
    assert not list(directory.rglob('*cursor*.json'))
    response = s.post(url+'/api/crawl', json={'preview_id': tested['preview_id']})
    assert response.status_code == 202
    job_id = response.json()['results'][0]['id']
    site = json.loads((directory/'web-jobs'/job_id/'site.json').read_text())
    assert site['schema']['title'] == rule
    assert site['channel'] == tested['channel'] == 'html'
    assert site['item_selector'] == selector
    assert site['list_url'] == article
    assert s.post(url+'/api/crawl', json={'preview_id': tested['preview_id']}).status_code == 400


def test_public_request_checks_redirect_before_connecting(monkeypatch):
    from crawler.public_urls import public_get
    from types import SimpleNamespace

    monkeypatch.delenv('CRAWL_ALLOW_PRIVATE', raising=False)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '',
         ('127.0.0.1' if host == '127.0.0.1' else '8.8.8.8', 443))
    ])
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        assert kwargs['allow_redirects'] is False
        return SimpleNamespace(is_redirect=True, headers={'Location': 'http://127.0.0.1/'},
                               close=lambda: None)

    with pytest.raises(ValueError, match='内网'):
        public_get(get, 'https://example.com/')
    assert calls == ['https://example.com/']

def test_automatic_preview_falls_back_when_wordpress_has_no_body(service, source):
    url, session, _, _, _ = service
    article = source('wp-no-body') + '/story/1'
    result = session.post(url+'/api/preview', json={'url': article}).json()
    assert result['channel'] == 'html' and result['preview_id'], result
    assert result['issues'][0]['reason'] == 'auto_wp_no_article'
    assert result['samples'][0]['text']
    tested = session.post(url+'/api/preview', json={'url': article, 'rules': {'title': '//h1'}}).json()
    assert tested['channel'] == 'html' and tested['preview_id'], tested


def test_preview_scope_limits_wordpress_job_to_selected_collections(service, source):
    url, session, directory, _, _ = service
    base = source('wp-with-media')
    response = session.post(url+'/api/preview', json={
        'url': base, 'scope': {'mode': 'all', 'kinds': ['pages', 'reports', 'media']},
    })
    response.raise_for_status()
    result = response.json()
    assert result['preview_id'], result
    assert {sample['kind'] for sample in result['samples']} == {'pages', 'reports', 'media'}
    queued = session.post(url+'/api/crawl', json={'preview_id': result['preview_id']}).json()
    job_id = queued['results'][0]['id']
    site = json.loads((directory/'web-jobs'/job_id/'site.json').read_text())
    assert site['_scope_kinds'] == ['pages', 'reports', 'media']
    job = wait_for(lambda: finished(session, url, 1))[0]
    assert set(job['results']) == {'pages', 'reports', 'media'}
    assert job['saved'] == 6
    assert any(sample.get('mime') == 'image/png' for sample in result['samples'])


def test_preview_path_limits_formal_html_discovery(service, source):
    url, session, _, _, _ = service
    base = source('html')
    response = session.post(url+'/api/preview', json={
        'url': base+'/story/1', 'scope': {'mode': 'path', 'path': '/story/2'},
    })
    response.raise_for_status()
    result = response.json()
    assert result['preview_id'], result
    assert [sample['url'] for sample in result['samples']] == [base+'/story/2']
    session.post(url+'/api/crawl', json={'preview_id': result['preview_id']}).raise_for_status()
    job = wait_for(lambda: finished(session, url, 1))[0]
    assert set(job['results']) == {'posts'}
    assert job['saved'] == 1
    assert job['selection']['mode'] == 'path'
