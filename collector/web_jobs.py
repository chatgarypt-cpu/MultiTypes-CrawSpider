"""Durable, isolated web jobs. One scheduler per server; workers own their receipts."""
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

from crawler.config import DATA, ROOT, REQUEST_QUEUE_CHANNELS, site_kinds
from crawler.processes import signal_group
from crawler.public_urls import normalize_url

JOBS = Path(DATA) / 'web-jobs'
LOCK = threading.RLock()
PROCS = {}
STOPPING = {}
ACTIVE = {'queued', 'running'}
STALL_KILL = int(os.environ.get("CRAWL_STALL_KILL", "1200"))


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


def worker_pid(directory):
    proc = PROCS.get(directory.name)
    if proc is not None:
        return proc.pid if proc.poll() is None else None
    try:
        pid = int((directory / "worker.pid").read_text())
        if pid <= 1 or os.getpgid(pid) != pid:
            return None
        args = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                              capture_output=True, text=True, timeout=2, check=True).stdout.strip()
        if args.endswith(f"{Path(ROOT) / 'web_worker.py'} {directory}"):
            return pid
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def jobs():
    result = []
    for path in JOBS.glob('*/job.json'):
        j = read(path)
        if not j.get('id'):
            continue
        proc = PROCS.get(j['id'])
        if proc is not None and proc.poll() is not None:
            signal_group(proc.pid, signal.SIGKILL)
            PROCS.pop(j['id'], None)
        if j.get('status') == 'running' and not held(path.parent / 'worker.lock'):
            # Worker writes a durable terminal receipt before unlocking. Read it again.
            j = read(path)
            if j.get('status') == 'running' and time.time() - j.get('started_epoch', 0) > 10:
                stalled = j['id'] in STOPPING
                reason = '任务长时间无进展，已中断' if stalled else '任务进程已中断'
                message = reason + ('，可重新核对' if j.get('action') == 'verify' else '，可继续采集')
                j.update(status='interrupted', error=message)
                write(path, j)
        if j.get('status') != 'running':
            STOPPING.pop(j['id'], None)
        files = []
        for f in (path.parent / 'data' / 'csv').glob('*.csv'):
            files.append({'name': f.name, 'url': f'/api/jobs/{j["id"]}/files/{f.name}'})
        j['files'] = files
        j['log_url'] = f'/api/jobs/{j["id"]}/log'
        # Cursor snapshots are cheap and let progress survive a server restart.
        if not j.get('action'):
            cursors = [read(p) for p in (path.parent / 'data').glob('*/*cursor*.json')]
            j['saved'] = sum(int(c.get('saved') or 0) for c in cursors)
            j['can_register'] = (path.parent / 'site.json').is_file()
        else:
            j.setdefault('saved', 0)
        result.append(j)
    return sorted(result, key=lambda j: j['created'], reverse=True)


def submit(values, *, site=None):
    if not isinstance(values, list) or not values:
        raise ValueError('请至少填写一个域名，每行一个')
    results = []
    with LOCK:
        active = {urlsplit(j['url']).netloc: j for j in jobs()
                  if j['status'] in ACTIVE and j.get('action') in (None, 'register')}
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
                directory = job_dir(j['id'])
                if site is not None:
                    write(directory / 'site.json', site)
                write(directory / 'job.json', j)
                active[domain] = j
                results.append({'input': value, 'status': 'queued', 'id': j['id'], 'message': '已加入队列'})
            except (ValueError, UnicodeError) as exc:
                results.append({'input': str(value), 'status': 'invalid', 'message': str(exc)})
    return results


def registered_site(domain, catalog=None):
    if not isinstance(domain, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.:-]*', domain):
        raise ValueError('站点域名无效')
    if catalog is None:
        from status import source_cfg
        catalog = source_cfg()
    site = catalog.get(domain)
    if not site or site.get('disabled') or not site.get('base') or not site.get('channel'):
        raise ValueError('站点未配置或已禁用')
    return site


def require_backfill_enabled(site, kinds):
    from supervisor import read_skip
    if (Path(DATA) / 'CRAWL_PAUSED').exists():
        raise ValueError('采集已全局暂停，请先恢复采集')
    skipped = read_skip(Path(DATA) / 'CRAWL_SKIP')
    if any(f"{site['domain']}:{kind}" in skipped for kind in kinds):
        raise ValueError('所选集合已暂停，请先解除对应暂停')
    if site.get('channel') == 'html' and not (site.get('list_url') or site.get('list_urls')):
        raise ValueError('请先配置站点的列表入口')


def action_receipt(job):
    keys = ('id', 'action', 'status', 'stage', 'created', 'requested_at', 'saved', 'error')
    return {key: job[key] for key in keys if key in job}


def prepare_site_action(action, site, kinds, mode):
    job = {'id': uuid.uuid4().hex, 'url': site['base'], 'domain': site['domain'],
           'action': action, 'status': 'queued', 'created': time.time(), 'saved': 0,
           'stage': '等待核对' if action == 'verify' else '等待补采'}
    if action == 'backfill':
        allowed = site_kinds(site)
        if (not isinstance(kinds, list) or not kinds or
                not all(isinstance(kind, str) and re.fullmatch(r'[a-z][a-z0-9_-]*', kind)
                        and kind in allowed for kind in kinds)):
            raise ValueError('请选择该站点已配置的采集集合')
        if mode not in ('resume', 'retry', 'rescan'):
            raise ValueError('请选择有效的补采方式')
        if mode == 'retry' and site.get('channel') not in REQUEST_QUEUE_CHANNELS:
            raise ValueError('该采集通道没有逐链接队列，请选择继续采集或重新扫描')
        kinds = [kind for kind in allowed if kind in kinds]
        require_backfill_enabled(site, kinds)
        job.update(kinds=kinds, mode=mode)
    return job


def submit_site_action(request):
    from status import source_cfg

    action = request.get('action')
    if action not in ('verify', 'backfill'):
        raise ValueError('站点操作无效')
    batch = 'targets' in request
    targets = request.get('targets') if batch else [request]
    if not isinstance(targets, list) or not targets or not all(isinstance(target, dict) for target in targets):
        raise ValueError('请选择至少一个站点')
    catalog = source_cfg()
    results = []
    with LOCK:
        active = {job['domain']: job for job in jobs() if job.get('action') and job['status'] in ACTIVE}
        for target in targets:
            domain = target.get('domain')
            result = {'domain': domain if isinstance(domain, str) else ''}
            try:
                site = registered_site(domain, catalog)
                job = prepare_site_action(action, site, target.get('kinds'), request.get('mode'))
                existing = active.get(domain)
                if existing is None:
                    write(job_dir(job['id']) / 'job.json', job)
                    active[domain] = job
                result.update(job=action_receipt(existing or job), duplicate=existing is not None)
            except (ValueError, OSError) as exc:
                if not batch:
                    raise
                result['error'] = str(exc)
            results.append(result)
    if batch:
        return {'results': results}
    return {'job': results[0]['job'], 'duplicate': results[0]['duplicate']}


def latest_site_actions(entries):
    def priority(job):
        return job['status'] in ACTIVE, job.get('requested_at', job['created'])

    result = {}
    for job in entries:
        if job.get('action') in ('verify', 'backfill'):
            actions = result.setdefault(job['domain'], {})
            previous = actions.get(job['action'])
            if previous is None or priority(job) > priority(previous):
                actions[job['action']] = action_receipt(job)
    return result


def submit_registration(source_id):
    with LOCK:
        directory = job_dir(source_id)
        source = read(directory / 'job.json')
        site = read(directory / 'site.json')
        if not source or source.get('action') or not site.get('domain') or not site.get('base'):
            raise ValueError('试采任务尚未识别站点')
        entries = jobs()
        previous = next((job for job in entries if job.get('source_job') == source_id), None)
        if previous and previous['status'] in ACTIVE | {'finished'}:
            return {'job': action_receipt(previous), 'duplicate': True}
        job = read(job_dir(previous['id']) / 'job.json') if previous else {
            'id': uuid.uuid4().hex, 'action': 'register', 'source_job': source_id,
            'url': site['base'], 'domain': site['domain'], 'created': time.time(), 'saved': 0,
        }
        job.update(status='queued', stage='等待加入长期采集', error='')
        write(job_dir(job['id']) / 'job.json', job)
        return {'job': action_receipt(job), 'duplicate': False}


def resume(job_id):
    with LOCK:
        path = job_dir(job_id) / 'job.json'
        j = read(path)
        if not j:
            raise ValueError('任务不存在')
        if j['status'] in ACTIVE or held(path.parent / 'worker.lock'):
            raise ValueError('任务已经在队列中或正在运行')
        entries = jobs()
        if any(x.get('source_job') == job_id and x['status'] in ACTIVE | {'finished'} for x in entries):
            raise ValueError('试采任务正在转入或已加入长期采集')
        if j.get('action') == 'register' and j['status'] == 'finished':
            raise ValueError('站点已加入长期采集')
        domain = urlsplit(j['url']).netloc
        if any(x['status'] in ACTIVE and bool(x.get('action')) == bool(j.get('action'))
               and (x.get('domain') == j['domain'] if j.get('action') else urlsplit(x['url']).netloc == domain)
               for x in entries):
            raise ValueError('该域名已有其他任务运行中')
        j.update(status='queued', stage='等待继续', error='', requested_at=time.time())
        write(path, j)


def _registration_ready(job, by_id):
    source = by_id.get(job['source_job'])
    if not source:
        return True
    directory = job_dir(source['id'])
    if source['status'] == 'queued':
        path = directory / 'job.json'
        receipt = read(path)
        receipt.update(status='interrupted', stage='转入长期采集', error='')
        write(path, receipt)
        source['status'] = 'interrupted'
    if source['status'] == 'running':
        pid = worker_pid(directory)
        if pid and held(directory / 'worker.lock') and job.get('stop_requested_pid') != pid:
            path = job_dir(job['id']) / 'job.json'
            receipt = read(path)
            signal_group(pid, signal.SIGTERM)
            receipt.update(stage='等待试采停止', stop_requested_pid=pid)
            write(path, receipt)
        return False
    return not held(directory / 'worker.lock')


def tick():
    with LOCK:
        entries = jobs()
        by_id = {job['id']: job for job in entries}
        waiting = {job['id'] for job in entries if job['status'] == 'queued'
                   and job.get('action') == 'register' and not _registration_ready(job, by_id)}
        for job in entries:
            if job["status"] == "running":
                _watch_job(job)
        n = int(read(Path(DATA) / 'queue_config.json', {'concurrency': 4}).get('concurrency', 4))
        available = max(0, n - sum(j['status'] == 'running' for j in entries))
        for j in sorted(entries, key=lambda x: x['created']):
            if available <= 0:
                break
            if j['status'] != 'queued' or j['id'] in waiting:
                continue
            directory = job_dir(j['id'])
            stage = {'verify': '正在核对', 'backfill': '正在准备补采',
                     'register': '正在合并已采数据'}.get(j.get('action'), '识别站点')
            j.update(status='running', stage=stage, started_epoch=time.time())
            for key in ('files', 'log_url', 'can_register'):
                j.pop(key, None)
            if not j.get('action'):
                j.pop('saved', None)
            write(directory / 'job.json', j)
            try:
                with open(directory / 'run.log', 'a') as log:
                    target = DATA if j.get('action') else directory / 'data'
                    env = dict(os.environ, CRAWL_DATA_DIR=str(target), PYTHONUNBUFFERED='1')
                    proc = subprocess.Popen([sys.executable, str(Path(ROOT) / 'web_worker.py'), str(directory)],
                                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True)
                    PROCS[j['id']] = proc
            except OSError as exc:
                j.update(status='failed', error=str(exc))
                write(directory / 'job.json', j)
            available -= 1


def _watch_job(job):
    directory = job_dir(job["id"])
    if job["id"] in STOPPING:
        pid, deadline = STOPPING[job["id"]]
        if time.monotonic() >= deadline and worker_pid(directory) == pid:
            signal_group(pid, signal.SIGKILL)
        return
    last = max(float(job.get("progress_epoch") or 0), float(job.get("started_epoch") or 0))
    try:
        last = max(last, (directory / "run.log").stat().st_mtime)
    except FileNotFoundError:
        pass
    if time.time() - last < STALL_KILL:
        return
    pid = worker_pid(directory)
    if pid is not None:
        STOPPING[job["id"]] = pid, time.monotonic() + 5
        signal_group(pid, signal.SIGTERM)


def scheduler(stop):
    while not stop.is_set():
        try:
            tick()
        except Exception:
            import traceback
            traceback.print_exc()
        stop.wait(2)
