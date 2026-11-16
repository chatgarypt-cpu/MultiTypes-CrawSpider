"""Resumable Directus-style JSON collection through the configured transport."""
import hashlib
import json
import logging
import time
from urllib.parse import parse_qsl, urlencode

from crawler import cursor
from crawler.mapping import map_json_item
from crawler.pipeline import normalize, validate
from crawler.store import append_rows, load_known
from crawler.web import Fetcher, parse_json_tolerant


def page_url(site, page=1, totals=False):
    params = dict(parse_qsl(site.get('api_query') or 'limit=100&meta=total_count', keep_blank_values=True))
    params['page'] = str(page)
    if totals:
        params.update(limit='1', meta='total_count,filter_count')
    return site['base'].rstrip('/') + site['api'] + '?' + urlencode(params)


def collect_json(site, limit=0, workers=8, progress=True, kind='posts', fetcher=None):
    domain = site['domain']
    fetcher = fetcher or Fetcher(site)
    cur = cursor.load(domain, kind)
    if cursor.is_complete(domain, kind):
        return dict(domain=domain, saved=0, status='complete', skipped='done')
    known = load_known(domain, kind)
    log = logging.getLogger(domain)
    prefix_map = None
    cat_cfg = site.get('api_category') or {}
    if cat_cfg.get('api'):
        url = site['base'].rstrip('/') + cat_cfg['api'] + '?' + (cat_cfg.get('query') or '')
        _, status, body = next(iter(fetcher.get_many([url])))
        payload = parse_json_tolerant(body) if status == 200 else None
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            cursor.mark_interrupted(domain, kind, 'category_fetch_failed')
            return dict(domain=domain, saved=0, status='interrupted', error='category_fetch_failed')
        by_id = {c['id']: c for c in payload['data']}
        def prefix_map(cid):
            c = by_id.get(cid) or {}
            parent = c.get('parent')
            parent = by_id.get(parent) if isinstance(parent, int) else parent
            return f"{parent['name']}/{c['name']}" if isinstance(parent, dict) else c.get('name', '')
    saved, page = 0, int(cur.get('next_page') or 1)
    total, t0 = cur.get('total_posts'), time.time()
    reason, state = None, 'interrupted'
    previous = cur.get('last_page_hash')
    while True:
        _, status, body = next(iter(fetcher.get_many([page_url(site, page)])))
        data = parse_json_tolerant(body) if status == 200 else None
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            reason = f'http_{status}' if status != 200 else 'bad_json'
            state = 'blocked' if status in (401, 403, 429) else 'interrupted'
            break
        items = data['data']
        meta = data.get('meta') or {}
        total = meta.get('filter_count', meta.get('total_count', total))
        if not items:
            state = 'complete'
            break
        fingerprint = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
        if fingerprint == previous:
            reason = 'repeated_page'
            break
        rows, invalid = [], []
        for item in items:
            if 'status' in item and item['status'] != 'published':
                continue
            row = normalize(map_json_item(item, site, prefix_map), site)
            if row['url'] in known:
                continue
            ok, why = validate(row, site, kind)
            if not ok:
                invalid.append(why)
                continue
            rows.append(row)
        written = append_rows(domain, rows, kind) if rows else 0
        saved += written
        known.update(row['url'] for row in rows)
        if invalid:
            reason = 'invalid_rows:' + ','.join(sorted(set(invalid)))
            break
        page += 1
        previous = fingerprint
        cursor.save(domain, kind, status='interrupted', next_page=page,
                    last_page_hash=previous, saved=len(known), total_posts=total)
        if progress:
            log.info('%s json page=%d saved=%d total=%s', domain, page - 1, saved, total)
        if limit and saved >= limit:
            state, reason = 'paused', 'limit'
            break
    cursor.save(domain, kind, status=state, next_page=page, saved=len(known),
                total_posts=total, last_reason=reason)
    result = dict(domain=domain, channel='json-api', via=site.get('via') or 'direct',
                  saved=saved, total=total, next_page=page, status=state,
                  secs=round(time.time()-t0, 1))
    if reason and state != 'paused':
        result['error'] = reason
    return result


def run(site, limit, log, fetcher, kind='posts'):
    return collect_json(site, limit=limit, kind=kind, fetcher=fetcher)
