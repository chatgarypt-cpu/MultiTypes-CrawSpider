"""Durable, isolated web jobs. One scheduler per server; workers own their receipts."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

from crawler.config import DATA, ROOT

JOBS = Path(DATA) / 'web-jobs'
LOCK = threading.RLock()
PROCS = {}
ACTIVE = {'queued', 'running'}


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temp, path)


def normalize_url(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请输入域名或网址')
    value = value.strip()
    p = urlsplit(value if '://' in value else 'https://' + value)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('仅支持不含账号密码的 HTTP / HTTPS 网址')
    host = p.hostname.encode('idna').decode().lower().rstrip('.')
    if not re.fullmatch(r'[a-z0-9.-]+', host) or '..' in host:
        raise ValueError('域名格式不正确')
    port = p.port
    if port and port not in (80, 443) and os.getenv('CRAWL_ALLOW_PRIVATE') != '1':
        raise ValueError('公开站点仅支持 80 / 443 端口')
    if os.getenv('CRAWL_ALLOW_PRIVATE') != '1':
        try:
            addresses = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError('域名无法解析，请检查拼写或网络') from exc
        # macOS TUN resolvers may map public names into the RFC 2544 fake-IP range.
        # Literal private IPs remain forbidden. This is a trusted-operator service,
        # not a hardened arbitrary-URL public proxy.
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        fake = ipaddress.ip_network('198.18.0.0/15')
        if (literal and not literal.is_global) or any(
                not ipaddress.ip_address(a[4][0]).is_global and
                not (literal is None and ipaddress.ip_address(a[4][0]) in fake) for a in addresses):
            raise ValueError('不能提交本机或内网地址')
    authority = host + (f':{port}' if port else '')
    return urlunsplit((p.scheme, authority, p.path or '/', p.query, ''))


def job_dir(job_id):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise ValueError('任务编号无效')
    return JOBS / job_id


def held(path):
    with open(path, 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def jobs():
    result = []
    for path in JOBS.glob('*/job.json'):
        j = read(path)
        if not j.get('id'):
            continue
        proc = PROCS.get(j['id'])
        if proc is not None:
            proc.poll()  # reap exited workers
        if j.get('status') == 'running' and not held(path.parent / 'worker.lock'):
            # Worker writes a durable terminal receipt before unlocking. Read it again.
            j = read(path)
            if j.get('status') == 'running' and time.time() - j.get('started_epoch', 0) > 10:
                j.update(status='interrupted', error='任务进程已中断，可点击继续采集')
                write(path, j)
        files = []
        for f in (path.parent / 'data' / 'csv').glob('*.csv'):
            files.append({'name': f.name, 'url': f'/api/jobs/{j["id"]}/files/{f.name}'})
        j['files'] = files
        j['log_url'] = f'/api/jobs/{j["id"]}/log'
        # Cursor snapshots are cheap and let progress survive a server restart.
        cursors = [read(p) for p in (path.parent / 'data').glob('*/*cursor*.json')]
        j['saved'] = sum(int(c.get('saved') or 0) for c in cursors)
        result.append(j)
    return sorted(result, key=lambda j: j['created'], reverse=True)


def submit(values):
    if not isinstance(values, list) or not values:
        raise ValueError('请至少填写一个域名，每行一个')
    results = []
    with LOCK:
        active = {urlsplit(j['url']).netloc: j for j in jobs() if j['status'] in ACTIVE}
        seen = set()
        for value in values:
            try:
                url = normalize_url(value)
                domain = urlsplit(url).netloc
                if domain in seen:
                    results.append({'input': value, 'status': 'duplicate', 'message': '本批次重复，已合并'})
                    continue
                seen.add(domain)
                if domain in active:
                    results.append({'input': value, 'status': 'duplicate', 'id': active[domain]['id'], 'message': '该域名已有排队或运行中的任务'})
                    continue
                j = {'id': uuid.uuid4().hex, 'url': url, 'status': 'queued', 'created': time.time(), 'stage': '等待执行'}
                write(job_dir(j['id']) / 'job.json', j)
                active[domain] = j
                results.append({'input': value, 'status': 'queued', 'id': j['id'], 'message': '已加入队列'})
            except (ValueError, UnicodeError) as exc:
                results.append({'input': str(value), 'status': 'invalid', 'message': str(exc)})
    return results


def resume(job_id):
    with LOCK:
        path = job_dir(job_id) / 'job.json'
        j = read(path)
        if not j:
            raise ValueError('任务不存在')
        if j['status'] in ACTIVE or held(path.parent / 'worker.lock'):
            raise ValueError('任务已经在队列中或正在运行')
        domain = urlsplit(j['url']).netloc
        if any(urlsplit(x['url']).netloc == domain and x['status'] in ACTIVE for x in jobs()):
            raise ValueError('该域名已有其他任务运行中')
        j.update(status='queued', stage='等待继续', error='')
        write(path, j)


def tick():
    with LOCK:
        entries = jobs()
        n = int(read(Path(DATA) / 'queue_config.json', {'concurrency': 4}).get('concurrency', 4))
        available = max(0, n - sum(j['status'] == 'running' for j in entries))
        for j in sorted(entries, key=lambda x: x['created']):
            if available <= 0:
                break
            if j['status'] != 'queued':
                continue
            directory = job_dir(j['id'])
            j.update(status='running', stage='识别站点', started_epoch=time.time())
            for key in ('files', 'log_url', 'saved'):
                j.pop(key, None)
            write(directory / 'job.json', j)
            try:
                with open(directory / 'run.log', 'a') as log:
                    env = dict(os.environ, CRAWL_DATA_DIR=str(directory / 'data'), PYTHONUNBUFFERED='1')
                    proc = subprocess.Popen([sys.executable, str(Path(ROOT) / 'web_worker.py'), str(directory)],
                                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True)
                    PROCS[j['id']] = proc
            except OSError as exc:
                j.update(status='failed', error=str(exc))
                write(directory / 'job.json', j)
            available -= 1


def scheduler(stop):
    while not stop.is_set():
        try:
            tick()
        except Exception:
            import traceback
            traceback.print_exc()
        stop.wait(2)
