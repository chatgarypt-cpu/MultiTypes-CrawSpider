"""Run a web job in its own data directory using the existing crawler pipeline."""
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time
from urllib.parse import urljoin, urlsplit

from crawler.config import DATA, ROOT, site_kinds, sources_path
from web_jobs import read, write, normalize_url
from crawler.public_urls import public_get


def crawl_links(site, progress, separate=False):
    """Persistent same-host frontier. Save content before acknowledging each URL."""
    from crawler.store import collection_lock
    with collection_lock(site["domain"], wait=False):
        return _crawl_links(site, progress, separate)


def _crawl_links(site, progress, separate):
    from lxml import html
    from crawler.discover import ASSET, declared_feed_urls, parse_feed
    from crawler.extract import extract_best, extract_metadata
    from crawler.htmlpage import is_navigation_page, page_error, paywall_signal, structured_body
    from crawler.pipeline import normalize, validate
    from crawler.request import RequestQueue, canonical_key
    from crawler.store import append_rows, load_known
    from crawler import cursor, paywall
    from crawler.web import Fetcher
    from crawler.runner import _export

    domain = site['domain']
    paywall.reset_budget(site.get("paywall_budget"))
    paywall.reset_health()
    q = RequestQueue(domain, 'web-pages' if separate else 'posts')
    known = {canonical_key(url) for url in load_known(domain, 'posts')}
    seeds = [site['base'].rstrip('/') + '/', site.get('start_url'), site.get('list_url'),
             *(site.get('list_urls') or []), *(site.get('seed_urls') or [])]
    seeds = [urljoin(site['base'] + '/', u) for u in seeds if u and '{' not in u]
    if site.get('feed') and '{' not in site['feed']:
        feed = urljoin(site['base'].rstrip('/') + '/', site['feed'])
        if urlsplit(feed).netloc == domain:
            q.add_many([feed], channel='rss', label='feed')
    q.add_many([u for u in seeds if urlsplit(u).netloc == domain], channel='html')
    q.retry_transient(cooldown=0)
    site = dict(site, channel='html', via='direct', robots=True, documents=False, _public_host=domain)
    fetcher = Fetcher(site)
    initial = saved = len(known)
    render_error = ""
    rendered = set()

    def same_site_link(u):
        p = urlsplit(u)
        return (p.scheme in ('http', 'https') and p.netloc == domain and not ASSET.search(p.path)
                and not any(x in (p.path + '?' + p.query).lower()
                            for x in ('logout', 'wp-admin', 'wp-login', 'add-to-cart')))

    def inspect(url, body):
        doc = html.fromstring(body)
        links = []
        for href in doc.xpath('//a[@href]/@href | //link[@rel="next" and @href]/@href'):
            if not href.strip() or href.lstrip().startswith('#'):
                continue
            u = urljoin(url, href)
            if same_site_link(u):
                links.append(u)
        feeds = declared_feed_urls(url, doc)
        navigation = is_navigation_page(url, body, site, doc)
        restriction = paywall_signal(body, site.get('premium_marker', ''))
        row = None
        if not restriction and not navigation:
            row = extract_best(url, body, site, min_text=0) or extract_metadata(url, body, site)
        return links, feeds, row, restriction, navigation, bool(doc.xpath('//script'))

    while q.pending():
        req = q.next_batch(1)[0]
        _, status, body = next(iter(fetcher.get_many([req.url])), (req.url, 0, ''))
        rejection = page_error(body) if status == 200 else ''
        recovered = ''
        if status != 200 or rejection or not body:
            if status == 200:
                status = 403 if rejection == 'challenge' else 0
            # 读不到正文时按站点开关走 13ft 链路（Googlebot → 存档 / Freedium）
            status, body, recovered = paywall.recover(
                req.url, site, body if isinstance(body, str) else '', status)
            if not recovered:
                q.mark_error([req.key], {req.key: f'http_{status} {rejection}'})
                continue
            rejection = ''
        if req.label == 'feed':
            entries = parse_feed(body, req.url)
            if entries is None:
                q.mark_error([req.key], {req.key: 'extraction_failed invalid_feed'})
            else:
                q.add_many([u for u in entries if same_site_link(u)], channel='html')
                q.mark_skipped([req.key], 'discovery_feed')
        else:
            try:
                links, feeds, row, restriction, navigation, has_script = inspect(req.url, body)
                if (not links and not feeds and not restriction and not (row or {}).get('text')
                        and has_script and req.key not in rendered):
                    rendered.add(req.key)
                    try:
                        from crawler.browser_playwright import fetch_many
                        _, browser_status, browser_body = fetch_many([req.url], public_host=domain)[0]
                        if browser_status != 200 or page_error(browser_body):
                            raise ValueError(f'浏览器返回 HTTP {browser_status}')
                        links, feeds, row, restriction, navigation, _ = inspect(req.url, browser_body)
                        if not links and not feeds and not restriction and not (row or {}).get('text'):
                            raise ValueError('渲染后未发现链接或正文')
                    except Exception as exc:
                        render_error = (str(exc).splitlines() or [type(exc).__name__])[0]
                        q.mark_error([req.key], {req.key: f'render_unverified {render_error}'}, max_retries=1)
                        continue
                if restriction and not recovered:
                    # 200 但命中 premium_marker / 结构化访问限制：换 13ft 链路的正文重做一次
                    _, fresh, recovered = paywall.recover(req.url, site, body, status)
                    if recovered:
                        body = fresh
                        links, feeds, row, restriction, navigation, _ = inspect(req.url, body)
                q.add_many(feeds, channel='rss', label='feed')
                q.add_many(links, channel='html')
                if restriction and structured_body(body):
                    restriction = ''          # 前端墙：JSON 里有全文
                if restriction:
                    q.mark_skipped([req.key], 'access_restricted')
                elif navigation:
                    q.mark_skipped([req.key], 'discovery_page')
                elif row:
                    row = normalize(row, site)
                    ok, reason = validate(row, site, 'posts')
                    if ok:
                        key = canonical_key(row['url'])
                        if key not in known:
                            saved += append_rows(domain, [row], 'posts')
                            known.add(key)
                        q.mark_done([req.key])
                    else:
                        q.mark_skipped([req.key], reason)
                else:
                    q.mark_skipped([req.key], 'no_content')
            except (ValueError, TypeError, html.etree.ParserError) as exc:
                q.mark_error([req.key], {req.key: f'extraction_failed {exc}'})
        stats = q.stats()
        cursor.save(domain, saved=saved)
        progress(stage=f'遍历站内链接 · 已发现 {sum(stats.values())} 页', scope='同域可访问的网页和分页；覆盖范围由实际发现的链接决定')
    stats = q.stats()
    skipped = dict(q.conn.execute(
        "SELECT error,count(*) FROM requests WHERE domain=? AND kind=? AND state='skipped' GROUP BY error",
        (domain, q.kind)).fetchall())
    unresolved = any(key not in ('discovery_page', 'discovery_feed') for key in skipped)
    state = 'partial' if stats.get('error') or unresolved or not saved else 'complete'
    if not separate:
        cursor.save(domain, saved=saved, status=state, queue=stats)
    error = _export(site, 'posts')
    return {'status': state, 'saved': saved-initial if separate else saved, 'queue': stats,
            'skipped': skipped, 'render_error': render_error, 'export_error': error}


def resolve_site(url, *, check_entry=False, channel="auto"):
    """Resolve the site identically for a preview and a new dashboard job."""
    import requests
    import yaml
    from crawler.adhoc import detect
    from crawler.web import PUBLIC_ROBOTS

    sources = yaml.safe_load(sources_path(ROOT).read_text()) or []
    if isinstance(sources, dict):
        sources = sources.get('sites', [])
    host = urlsplit(url).netloc
    site = next((dict(s) for s in sources if s.get('domain') == host and not s.get('disabled')), None)
    if site is not None and channel == 'auto':
        return site

    with public_get(requests.get, url, timeout=25, stream=True,
                    allow_target=PUBLIC_ROBOTS.allowed) as response:
        base_url = normalize_url(response.url)
        status = response.status_code
    if check_entry and status >= 400:
        raise ValueError(f'起始页返回 HTTP {status}')
    if channel != 'auto':
        base = urlsplit(base_url)
        return {'domain': base.netloc, 'base': f'{base.scheme}://{base.netloc}',
                'channel': channel, 'via': 'direct', 'start_url': base_url,
                '_public_host': base.netloc}
    site = detect(base_url, public=True)
    site.update(start_url=base_url, _automatic=True, _public_host=site['domain'])
    return site


def job_site(site):
    result = dict(site, discover_cap=0)
    if site.get('channel') == 'json-list':
        result.update(max_pages_per_run=0, scan_mode='full')
    return result


def run_site_action(job, update):
    from contextlib import closing
    from crawler.request import RequestQueue
    from crawler.runner import collect_site, reset_progress
    from crawler.store import CollectionBusy, collection_lock
    from web_jobs import registered_site, require_backfill_enabled

    site = registered_site(job['domain'])
    if job['action'] == 'verify':
        from verify_coverage import verify_and_store
        update(stage='正在核对覆盖率')
        record = verify_and_store(site, not_before=job.get('requested_at', job['created']))
        failed = record['verdict'] == '核对失败'
        update(status='failed' if failed else 'finished', stage=record['verdict'],
               error=(record.get('verdict_detail') or record.get('source') or '核对失败') if failed else '',
               finished=time.time())
        return

    kinds, mode = job['kinds'], job['mode']
    if any(kind not in site_kinds(site) for kind in kinds):
        raise ValueError('站点的采集集合已变化，请重新选择')
    prepared = dict(job.get('prepared') or {})
    results = dict(job.get('results') or {})
    update(channel=site['channel'])
    for kind in kinds:
        if results.get(kind, {}).get('status') == 'complete' and not results[kind].get('error'):
            continue
        update(stage=f'补采 {kind}')
        try:
            require_backfill_enabled(site, [kind])
            with collection_lock(site['domain'], kind, wait=False):
                if kind not in prepared:
                    requeued = 0
                    if mode == 'retry':
                        queue = RequestQueue(site['domain'], kind)
                        with closing(queue.conn):
                            requeued = queue.retry_transient(cooldown=0, statuses=site.get('retry_http_statuses') or ())
                            requeued += queue.requeue_failed(
                                ('no_text', 'short_text', 'extraction_failed', 'render_unverified'))['requeued']
                    elif mode == 'rescan':
                        reset_progress(site, kind)
                    prepared[kind] = requeued
                    update(prepared=prepared)
                results[kind] = collect_site(site, 0, kind, export_csv=False)
                results[kind]['requeued'] = prepared[kind]
        except CollectionBusy:
            results[kind] = {'status': 'interrupted', 'error': '该集合正在采集，请等当前任务结束后再试'}
        except (OSError, ValueError, RuntimeError) as exc:
            results[kind] = {'status': 'failed', 'error': str(exc)}
        update(results=results, saved=sum(result.get('saved', 0) for result in results.values()))
    errors = [result['error'] for result in results.values() if result.get('error')]
    incomplete = any(result.get('status') != 'complete' for result in results.values())
    update(status='failed' if len(errors) == len(kinds) else 'partial' if incomplete or errors else 'finished',
           stage='补采本轮结束', error='；'.join(dict.fromkeys(errors)), finished=time.time())


def run(directory):
    directory = Path(directory)
    path = directory / 'job.json'
    with open(directory / 'worker.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (directory / 'worker.pid').write_text(str(os.getpid()))
        job = read(path)
        def update(**fields):
            job.update(progress_epoch=time.time(), **fields)
            write(path, job)
        def stop(*_):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, stop)
        try:
            if job.get('action') == 'register':
                from registration import register
                register(job, update)
                return
            if job.get('action'):
                run_site_action(job, update)
                return

            from crawler.runner import collect_site
            from crawler.web import Fetcher
            from crawler.discover import wp_type_inventory
            import requests

            os.makedirs(DATA, exist_ok=True)
            site = read(directory / 'site.json')
            if not site:
                site = job_site(resolve_site(job['url']))
                write(directory / 'site.json', site)
            update(domain=site['domain'], channel=site['channel'], selection=site.get('_selection'))
            results = {}
            warnings = []
            discovery_incomplete = False
            link_scan = site.get('_automatic') and site['channel'] in ('html', 'rss')

            def collect(kind):
                try:
                    return collect_site(site, 0, kind)
                except (ImportError, RuntimeError, requests.RequestException) as exc:
                    return {'status': 'interrupted', 'error': str(exc)}

            if site['channel'] == 'wp-api':
                update(stage='发现公开内容类型', scope='WordPress 公开 REST 内容集合；媒体为元数据，附件原文件不下载')
                if not site.get('_scope_kinds'):
                    try:
                        inventory = wp_type_inventory(site, Fetcher(site))
                    except (ImportError, RuntimeError, requests.RequestException) as exc:
                        inventory = {'error': str(exc)}
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
                for kind in site.get('_scope_kinds') or site_kinds(site):
                    update(stage=f'采集 {kind}', results=results)
                    results[kind] = collect(kind)
            elif link_scan:
                # RSS generally only contains recent items. Traverse the site instead of claiming history is complete.
                results['posts'] = crawl_links(site, update)
            else:
                update(stage='采集已发现页面', scope=(site.get('_selection') or {}).get('label') or '站点地图或已有适配器覆盖的公开内容；尚无全站独立总量核对')
                results['posts'] = collect('posts')
            incomplete_text = any(results.get(kind, {}).get('status') in ('blocked', 'interrupted', 'partial')
                                  for kind in ('posts', 'pages'))
            if not link_scan and not site.get('_restricted_scope') and (discovery_incomplete or incomplete_text):
                update(stage='核对公开网页和栏目', results=results)
                results['web_pages'] = crawl_links(dict(site, start_url=job['url']), update, separate=True)
            bad = discovery_incomplete or any(r.get('status') != 'complete' or r.get('error') or r.get('export_error') for r in results.values())
            # Collection completion is distinct from independently verified whole-site coverage.
            update(status='partial' if bad else 'finished', stage='本轮结束', results=results,
                   warnings=warnings, finished=time.time(),
                   error='部分内容未采完，请查看各集合结果或日志后继续。' if bad else '')
        except KeyboardInterrupt:
            if job.get('action') != 'verify':
                export_available(locals().get('site'))
            update(status='interrupted', stage='已停止', error='任务已停止，可在任务队列中继续。')
        except Exception as exc:
            import traceback
            traceback.print_exc()
            export_available(locals().get('site'))
            stage = {'verify': '核对失败', 'register': '加入长期采集失败'}
            update(status='failed', stage=stage.get(job.get('action'), '采集失败'),
                   error=str(exc), finished=time.time())


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
