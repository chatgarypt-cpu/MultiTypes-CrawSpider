"""Move an isolated web collection into the operator's catalog and queue."""
from contextlib import ExitStack, closing
import fcntl
from itertools import batched
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit

import yaml

from crawler.config import DATA, ROOT, REQUEST_QUEUE_CHANNELS, catalog_path, site_kinds, stored_kinds
from crawler.request import RequestQueue, canonical_key
from crawler.schema import find_duplicate_keys, validate_site
from crawler.store import _recover_append, append_rows, collection_lock, load_known, save_cursor


def registration_path(job_id, data_dir=DATA):
    return Path(data_dir) / 'supervisor-inbox' / f'{job_id}.json'


def _saved_site(site):
    runtime = {'_automatic', '_selection', '_restricted_scope', 'start_url', 'discover_cap'}
    return {key: value for key, value in site.items() if key not in runtime}


def _append_site(catalog, text, configured, site):
    entry = yaml.safe_dump([site], allow_unicode=True, sort_keys=False)
    node = yaml.compose(text)
    if node is None:
        updated = text.rstrip() + '\n' + entry
    elif not isinstance(node, yaml.SequenceNode) or node.flow_style:
        updated = (text[:node.start_mark.index] + '\n' +
                   yaml.safe_dump([*configured, site], allow_unicode=True, sort_keys=False) +
                   text[node.end_mark.index:])
    else:
        end = node.end_mark.index
        updated = text[:end].rstrip() + '\n' + entry + text[end:]
    if find_duplicate_keys(updated) or yaml.safe_load(updated) != [*configured, site]:
        raise ValueError('信源文件无法安全追加')
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=catalog.parent, delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
            if catalog.exists():
                os.chmod(temp, catalog.stat().st_mode & 0o777)
            os.replace(temp, catalog)
        finally:
            temp.unlink(missing_ok=True)


def _paths(data, domain, kind):
    directory = data / domain
    return (directory / ('articles.jsonl' if kind == 'posts' else f'{kind}.jsonl'),
            directory / ('urls.txt' if kind == 'posts' else f'{kind}.urls.txt'),
            directory / ('cursor.json' if kind == 'posts' else f'{kind}.cursor.json'))


def _merge_requests(source, domain, kind, known, channel):
    queue = RequestQueue(domain, kind)
    with closing(queue.conn):
        queue.seed_done(known.values(), channel)
        before = queue.stats().get('unprocessed', 0)
        db = source / 'state.db'
        if db.is_file():
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as original:
                if original.execute("SELECT 1 FROM sqlite_master WHERE name='requests'").fetchone():
                    records = original.execute(
                        'SELECT key,url,channel,label,state,retries,error,created,updated '
                        'FROM requests WHERE domain=? AND kind=?', (domain, kind))
                    with queue.conn:
                        queue.conn.executemany(
                            'INSERT OR IGNORE INTO requests(domain,kind,key,url,channel,label,state,retries,error,created,updated) '
                            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            ((domain, kind, key, url, route, label,
                              'unprocessed' if state in ('done', 'inflight') else state, retries, error, created, updated)
                             for key, url, route, label, state, retries, error, created, updated in records
                             if key not in known and label not in ('discovery_page', 'discovery_feed')))
        return queue.stats().get('unprocessed', 0) - before


def _move_collection(source, target, site, kind, same_rules):
    domain, channel = site['domain'], site['channel']
    article, index, checkpoint = _paths(source, domain, kind)
    _, _, target_checkpoint = _paths(target, domain, kind)
    _recover_append(str(article), str(index))
    saved = 0
    if article.is_file():
        with article.open(encoding='utf-8') as stream:
            for rows in batched((json.loads(line) for line in stream if line.strip()),
                                max(1, int(os.environ.get('QUEUE_BATCH', '200')))):
                saved += append_rows(domain, rows, kind)
    known = {canonical_key(url): url for url in load_known(domain, kind)}
    pending = 0
    if same_rules and channel in REQUEST_QUEUE_CHANNELS and kind in site_kinds(site):
        pending = _merge_requests(source, domain, kind, known, channel)

    checkpoint_to_keep = target_checkpoint if target_checkpoint.is_file() else checkpoint if same_rules else None
    if checkpoint_to_keep is not None and checkpoint_to_keep.is_file():
        cursor = json.loads(checkpoint_to_keep.read_text(encoding='utf-8'))
        if checkpoint_to_keep != target_checkpoint or cursor.get('saved') != len(known):
            save_cursor(domain, dict(cursor, saved=len(known)), kind)
    if same_rules and channel == 'json-list' and kind == 'posts':
        source_list = checkpoint.with_name('jsonlist.cursor.json')
        target_list = target_checkpoint.with_name('jsonlist.cursor.json')
        if checkpoint_to_keep == checkpoint and source_list.is_file() and not target_list.exists():
            from web_jobs import write
            write(target_list, json.loads(source_list.read_text(encoding='utf-8')))
    return {'status': 'complete', 'saved': saved, 'pending': pending}


def register(job, update):
    from web_jobs import ACTIVE, held, job_dir, read, write

    directory = job_dir(job['source_job'])
    source_job = read(directory / 'job.json')
    if not source_job or source_job['status'] in ACTIVE or held(directory / 'worker.lock'):
        raise ValueError('试采任务仍在运行')
    source = directory / 'data'
    target = Path(DATA)
    detected = read(directory / 'site.json')
    domain = job['domain']
    base = urlsplit(detected.get('base', ''))
    if (detected.get('domain') != domain or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.:-]*', domain)
            or base.netloc != domain or base.scheme not in ('http', 'https')):
        raise ValueError('任务缺少有效的站点配置')
    catalog = catalog_path(ROOT)
    catalog.parent.mkdir(parents=True, exist_ok=True)
    with catalog.with_name(catalog.name + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        text = catalog.read_text(encoding='utf-8') if catalog.exists() else ''
        if find_duplicate_keys(text):
            raise ValueError('信源文件有重复字段或语法错误')
        configured = yaml.safe_load(text) or []
        if not isinstance(configured, list) or any(not isinstance(site, dict) for site in configured):
            raise ValueError('信源文件须使用站点列表')
        existing = next((site for site in configured if site.get('domain') == domain), None)
        site = existing if existing is not None else _saved_site(detected)
        if existing is None:
            if site.get('via', 'direct') != 'direct':
                raise ValueError('请先确认正式信源的采集出口')
            site['_public_host'] = domain
        errors, _ = validate_site(site)
        if errors or site.get('disabled'):
            raise ValueError('站点配置无效或已停用' + ('：' + '；'.join(errors) if errors else ''))
        same_rules = _saved_site(detected) == _saved_site(site)
        if existing is None:
            same_rules = True
        kinds = tuple(kind for kind in site_kinds(site)
                      if not detected.get('_scope_kinds') or kind in detected['_scope_kinds'])
        if not kinds:
            raise ValueError('试采范围与正式信源没有共同集合')
        extra = [kind for kind in stored_kinds(source / domain)
                 if re.fullmatch(r'[a-z][a-z0-9_-]*', kind)
                 and _paths(source, domain, kind)[0].is_file()]
        results = dict(job.get('results') or {})
        update(channel=site['channel'])
        with ExitStack() as locks:
            for kind in sorted(set(kinds) | set(extra)):
                locks.enter_context(collection_lock(domain, kind, data_dir=source, wait=False))
                locks.enter_context(collection_lock(domain, kind, data_dir=target, wait=False))
            for kind in dict.fromkeys((*kinds, *extra)):
                update(stage=f'合并 {kind}')
                result = _move_collection(source, target, site, kind, same_rules)
                for key in ('saved', 'pending'):
                    result[key] += results.get(kind, {}).get(key, 0)
                results[kind] = result
                update(results=results, saved=sum(item['saved'] for item in results.values()))
                print(f"register {domain} {kind} saved={result['saved']} pending={result['pending']}", flush=True)
            if existing is None:
                _append_site(catalog, text, configured, site)
            write(registration_path(job['id'], target), {'domain': domain, 'kinds': kinds})
    update(status='finished', stage='已加入长期采集', finished=time.time())
