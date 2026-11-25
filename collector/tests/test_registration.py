from contextlib import closing
import json
import signal
import time
from types import SimpleNamespace

import pytest
import yaml

from crawler.request import RequestQueue
from crawler.store import append_rows
import registration
import supervisor
import web_jobs


@pytest.mark.parametrize('existing', [False, True])
def test_registration_reuses_catalog_and_merges_resumable_state(tmp_path, tmp_env, monkeypatch, existing):
    domain = 'news.test'
    site = {'domain': domain, 'base': 'https://news.test', 'channel': 'sitemap', 'sitemap': '/sitemap.xml'}
    if existing:
        site['name'] = '主库'
    catalog = tmp_path / 'sources.yaml'
    text = '# Keep this comment\n' + (yaml.safe_dump([site], allow_unicode=True) if existing else '[]\n')
    catalog.write_text(text)
    for module, name, value in (
        (registration, 'ROOT', str(tmp_path)), (registration, 'DATA', str(tmp_env)),
        (web_jobs, 'DATA', str(tmp_env)), (web_jobs, 'JOBS', tmp_env / 'web-jobs'),
        (web_jobs, 'PROCS', {}), (web_jobs, 'STOPPING', {}),
    ):
        monkeypatch.setattr(module, name, value)
    source_id = 'a' * 32
    directory = web_jobs.job_dir(source_id)
    source = directory / 'data'
    source_site = source / domain
    source_site.mkdir(parents=True)
    web_jobs.write(directory / 'job.json', {
        'id': source_id, 'status': 'interrupted', 'created': 1, 'url': site['base'],
    })
    web_jobs.write(directory / 'site.json', dict(site, discover_cap=0))
    known, new, remaining, gone = [f'{site["base"]}/{suffix}' for suffix in ('known', 'new', 'pending', 'gone')]
    append_rows(domain, [{'url': known, 'title': '主库原文'}])
    (source_site / 'articles.jsonl').write_text(''.join(
        json.dumps({'url': url, 'title': '试采正文'}) + '\n' for url in (known, new)))
    (source_site / 'urls.txt').write_text(f'{known}\n{new}\n')
    web_jobs.write(source_site / 'cursor.json', {'status': 'interrupted', 'next_page': 2, 'saved': 2})
    requests = RequestQueue(domain, db_path=str(source / 'state.db'))
    with closing(requests.conn):
        requests.seed_done([known, new], 'sitemap')
        requests.add_many([remaining, gone], 'sitemap')
        requests.mark_error([gone], {gone: 'http_404 missing'}, max_retries=1)

    receipt = web_jobs.submit_registration(source_id)['job']
    job_path = web_jobs.job_dir(receipt['id']) / 'job.json'
    job = web_jobs.read(job_path)
    registration.register(job, lambda **changes: job.update(changes))
    web_jobs.write(job_path, job)

    assert job['status'] == 'finished' and job['saved'] == 1
    assert job['results']['posts']['pending'] == 1
    assert catalog.read_text().startswith('# Keep this comment\n')
    if existing:
        assert catalog.read_text() == text
    else:
        assert yaml.safe_load(catalog.read_text()) == [dict(site, _public_host=domain)]
    assert json.loads((tmp_env / domain / 'cursor.json').read_text())['next_page'] == 2
    rows = [json.loads(line) for line in (tmp_env / domain / 'articles.jsonl').read_text().splitlines()]
    assert [row['url'] for row in rows] == [known, new] and rows[0]['title'] == '主库原文'
    queue = RequestQueue(domain)
    with closing(queue.conn):
        assert queue.stats() == {'done': 2, 'unprocessed': 1, 'error': 1}
        assert [row.url for row in queue.next_batch()] == [remaining]
    request = registration.registration_path(job['id'], tmp_env)
    assert json.loads(request.read_text()) == {'domain': domain, 'kinds': ['posts']}
    assert web_jobs.submit_registration(source_id)['duplicate']
    with pytest.raises(ValueError, match='长期采集'):
        web_jobs.resume(source_id)


def test_supervisor_accepts_registration_from_completed_state_only_once(tmp_env, monkeypatch):
    domain, kind = 'news.test', 'posts'
    key = (domain, kind)
    job_id = 'b' * 32
    path = registration.registration_path(job_id, tmp_env)
    payload = {'domain': domain, 'kinds': [kind]}
    web_jobs.write(path, payload)
    state = tmp_env / 'supervise_state.json'
    web_jobs.write(state, {'pending': [], 'completed': [f'{domain}:{kind}']})
    monkeypatch.setenv('CRAWL_RESUME_STATE', '1')
    handlers, starts, ticks = {}, [], []

    def start(item, _running):
        assert job_id in json.loads(state.read_text())['registrations']
        starts.append(item)

    def sleep(_):
        ticks.append(1)
        if len(ticks) == 1:
            assert not path.exists()
            web_jobs.write(path, payload)
        else:
            handlers[signal.SIGTERM]()

    for name, value in {
        'DATA': tmp_env, 'STATE': state, 'SKIP': tmp_env / 'CRAWL_SKIP', 'PAUSE': tmp_env / 'CRAWL_PAUSED',
        'load_tasks': lambda: [key], 'start': start, 'is_done': lambda *_: False,
        '_write_health': lambda *_: None, 'log': lambda *_: None,
        'signal': SimpleNamespace(SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT,
                                  signal=lambda signum, handler: handlers.update({signum: handler})),
        'time': SimpleNamespace(time=time.time, monotonic=time.monotonic, strftime=time.strftime, sleep=sleep),
    }.items():
        monkeypatch.setattr(supervisor, name, value)
    assert supervisor.main() == 0
    assert starts == [key] and not path.exists()
