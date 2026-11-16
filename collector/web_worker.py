"""Run a web job in its own data directory using the existing crawler pipeline."""
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time
from urllib.parse import urljoin, urlsplit

from crawler.config import DATA, ROOT, site_kinds
from web_jobs import read, write, normalize_url


def crawl_links(site, progress):
    """Persistent same-host frontier. Save content before acknowledging each URL."""
    from lxml import html
    from crawler.discover import ASSET
    from crawler.extract import extract_best, extract_metadata
    from crawler.htmlpage import page_error
    from crawler.pipeline import normalize, validate
    from crawler.request import RequestQueue, canonical_key
    from crawler.store import append_rows, load_known, save_cursor
    from crawler.web import Fetcher
    from crawler.runner import _export

    domain = site['domain']
    q = RequestQueue(domain, 'posts')
    known = load_known(domain, 'posts')
    q.add_many([site['base'] + '/', site.get('start_url', site['base'] + '/')], channel='html')
    q.retry_transient(cooldown=0)
    fetcher = Fetcher(dict(site, channel='html'))
    saved = len(known)
    while q.pending():
        req = q.next_batch(1)[0]
        _, status, body = next(iter(fetcher.get_many([req.url])))
        rejection = page_error(body) if status == 200 else ''
        if status != 200 or rejection:
            q.mark_error([req.key], {req.key: f'http_{status} {rejection}'})
        else:
            try:
                doc = html.fromstring(body)
                links = []
                for href in doc.xpath('//a[@href]/@href'):
                    u = canonical_key(urljoin(req.url, href))
                    p = urlsplit(u)
                    if p.scheme in ('http', 'https') and p.netloc == domain and not ASSET.search(p.path):
                        # Do not submit forms or follow action links; preserve ordinary pagination queries.
                        if not any(x in (p.path + '?' + p.query).lower() for x in ('logout', 'wp-admin', 'wp-login', 'add-to-cart')):
                            links.append(u)
                q.add_many(links, channel='html')
                row = extract_best(req.url, body, site, min_text=0) or extract_metadata(req.url, body, site)
                if row:
                    row = normalize(row, site)
                    ok, reason = validate(row, site, 'posts')
                    if ok:
                        if row['url'] not in known:
                            append_rows(domain, [row], 'posts')
                            known.add(row['url'])
                            saved += 1
                        q.mark_done([req.key])
                    else:
                        q.mark_skipped([req.key], reason)
                else:
                    q.mark_skipped([req.key], 'no_content')
            except (ValueError, TypeError) as exc:
                q.mark_error([req.key], {req.key: str(exc)})
        stats = q.stats()
        save_cursor(domain, {'saved': saved, 'status': 'running', 'done': False, 'queue': stats}, 'posts')
        progress(stage=f'遍历站内链接 · 已发现 {sum(stats.values())} 页', scope='同域公开 HTML 链接；不含登录内容、附件文件和仅由脚本生成的链接')
    stats = q.stats()
    state = 'partial' if stats.get('error') or stats.get('skipped') or not saved else 'complete'
    save_cursor(domain, {'saved': saved, 'status': state, 'done': state == 'complete', 'queue': stats}, 'posts')
    error = _export(site, 'posts')
    return {'status': state, 'saved': saved, 'queue': stats, 'export_error': error}


def run(directory):
    directory = Path(directory)
    path = directory / 'job.json'
    with open(directory / 'worker.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (directory / 'worker.pid').write_text(str(os.getpid()))
        job = read(path)
        def update(**fields):
            job.update(fields)
            write(path, job)
        def stop(*_):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, stop)
        try:
            from crawler.adhoc import detect
            from crawler.runner import collect_site
            from crawler.web import Fetcher
            from crawler.discover import wp_type_inventory
            import requests
            import yaml

            os.makedirs(DATA, exist_ok=True)
            site = read(directory / 'site.json')
            if not site:
                # Reuse curated adapters when a host is already known.
                sources = yaml.safe_load((Path(ROOT) / 'sources.yaml').read_text()) or []
                if isinstance(sources, dict):
                    sources = sources.get('sites', [])
                host = urlsplit(job['url']).netloc
                site = next((dict(s) for s in sources if s.get('domain') == host and not s.get('disabled')), None)
                if not site:
                    response = requests.get(job['url'], timeout=25)
                    base_url = normalize_url(response.url)
                    site = detect(base_url)
                    site['start_url'] = base_url
                    site['_automatic'] = True
                site['discover_cap'] = 0
                if site.get('channel') == 'json-list':
                    site['max_pages_per_run'] = 0
                    site['scan_mode'] = 'full'
                write(directory / 'site.json', site)
            update(domain=site['domain'], channel=site['channel'])
            results = {}
            warnings = []
            discovery_incomplete = False
            if site['channel'] == 'wp-api':
                update(stage='发现公开内容类型', scope='WordPress 公开 REST 内容集合；媒体为元数据，附件原文件不下载')
                inventory = wp_type_inventory(site, Fetcher(site))
                if inventory.get('error'):
                    warnings.append(inventory['error'])
                    discovery_incomplete = True
                else:
                    types = dict(site.get('wp_types') or {})
                    excluded = []
                    for entry in inventory['types']:
                        name = entry['type']
                        if name in ('post', 'page', 'attachment'):
                            continue
                        if re_safe_kind(name) and not name.startswith(('wp_', 'nav_')):
                            types[name] = entry['route']
                        else:
                            excluded.append(name)
                    site['wp_types'] = types
                    if excluded:
                        warnings.append('未采系统内容类型：' + ', '.join(excluded))
                    write(directory / 'site.json', site)
                for kind in site_kinds(site):
                    update(stage=f'采集 {kind}', results=results)
                    results[kind] = collect_site(site, 0, kind)
            elif site.get('_automatic') and site['channel'] in ('html', 'rss'):
                # RSS generally only contains recent items. Traverse the site instead of claiming history is complete.
                results['posts'] = crawl_links(site, update)
            else:
                update(stage='采集已发现页面', scope='站点地图或已有适配器覆盖的公开内容；尚无全站独立总量核对')
                results['posts'] = collect_site(site, 0, 'posts')
            bad = discovery_incomplete or any(r.get('status') != 'complete' or r.get('error') or r.get('export_error') for r in results.values())
            # Collection completion is distinct from independently verified whole-site coverage.
            update(status='partial' if bad else 'finished', stage='本轮结束', results=results,
                   warnings=warnings, finished=time.time(),
                   error='部分内容未采完，请查看各集合结果或日志后继续。' if bad else '')
        except KeyboardInterrupt:
            export_available(site)
            update(status='interrupted', stage='已停止', error='任务已停止，已保存的数据保留，可继续采集。')
        except Exception as exc:
            import traceback
            traceback.print_exc()
            export_available(locals().get('site'))
            update(status='failed', stage='采集失败', error=str(exc), finished=time.time())


def export_available(site):
    if not site:
        return
    from crawler.config import stored_kinds
    from crawler.runner import _export
    for kind in stored_kinds(Path(DATA) / site['domain']):
        _export(site, kind)


def re_safe_kind(name):
    import re
    return bool(re.fullmatch(r'[A-Za-z0-9_-]+', name))


if __name__ == '__main__':
    run(sys.argv[1])
