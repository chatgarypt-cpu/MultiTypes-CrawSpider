import html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests, trafilatura
from crawler.config import ROOT, DATA, DB, UA, COLS, MEDIA_COLS, WORKERS
from crawler import store, discover, mapping, extract as extract_mod, web
from crawler.store import (load_cursor, save_cursor, load_known, append_rows,
                           existing_urls, save, db, _paths)
from crawler.discover import sitemap_urls, rss_urls, html_urls, wp_page_url
from crawler.mapping import map_wp_post, map_wp_media, map_json_item, _flatten_rich
from crawler.extract import extract_article, extract_best
from crawler.web import Fetcher, _get, wp_json, cloud_get

def collect_json_list(site, limit=0):
    """List JSON -> detail page fetch -> rows (e.g. samoaobserver).

    Expects site config: api_list (/stories/page/{page}.json?category={category}),
    api_categories (list), api_items (key holding the array), api_url (detail template),
    free_field (skip items where item[field] is true).
    """
    import json as _json
    base = site["base"].rstrip("/")
    cats = site.get("api_categories") or [None]
    pat = site.get("api_list") or ""
    items_key = site.get("api_items") or "stories"
    url_tpl = site.get("api_url") or "{base}/item/{id}"
    free_key = site.get("free_field")
    known = load_known(site["domain"], "posts")
    lg = logging.getLogger(site["domain"])
    fetcher = Fetcher(site)
    cur_path = os.path.join(DATA, site["domain"], "jsonlist.cursor.json")
    cur = {}
    if os.path.exists(cur_path):
        try: cur = json.load(open(cur_path))
        except Exception: cur = {}
    start_cat = int(cur.get("cat_index", 0)); start_page = int(cur.get("page", 1))
    saved = 0; page = start_page; t0 = time.time()
    cat_list = list(cats)[start_cat:]
    cats = cat_list
    while page <= int(site.get("pages", 100000)):
        urls = []; page_has_items = False
        for cat in cats:
            u = pat.format(page=page, category=cat) if cat else pat.format(page=page)
            if u.startswith("/"):
                u = base + u
            try:
                r = requests.get(u, headers={"User-Agent": UA}, timeout=45)
                if r.status_code != 200: continue
                data = r.json()
            except Exception:
                continue
            items = data.get(items_key) or data.get("data") or []
            if items: page_has_items = True
            if not items: continue
            for it in items:
                if free_key and it.get(free_key): continue
                cat_name = it.get("category_name") or cat or "news"
                link = url_tpl.format(base=base, id=it.get("id"), category_name=cat_name, slug=it.get("slug") or "")
                if link in known: continue
                urls.append((link, it))
        if not urls:
            if page_has_items:
                page += 1
                try: json.dump({"page": page}, open(cur_path, "w"))
                except Exception: pass
                continue
            break
        rows = []
        meta = {u: it for u, it in urls}
        for u, status, body in fetcher.stream_many([x[0] for x in urls], workers=4):
            if status != 200 or not body: continue
            if site.get("premium_marker") and site["premium_marker"] in body: continue
            row = extract_best(u, body, site, min_text=120)
            if not row: continue
            it = meta.get(u, {})
            row["title"] = row["title"] or (it.get("heading") or "")
            row["created_at"] = row["created_at"] or (it.get("published_at_iso") or "")
            row["description"] = row["description"] or (it.get("summary") or "")[:500]
            if row["url"] in known: continue
            known.add(row["url"]); rows.append(row)
        if rows:
            written = append_rows(site["domain"], rows, "posts")
            saved += written if isinstance(written, int) else len(rows)
            lg.info("%s json-list page %s saved=%d", site["domain"], page, saved)
        try:
            json.dump({"page": page + 1}, open(cur_path, "w"))
        except Exception:
            pass
        if limit and saved >= limit: break
        page += 1
    return {"domain": site["domain"], "channel": "json-list", "saved": saved, "pages": page - 1,
            "total": saved, "secs": round(time.time() - t0, 1)}

def run(site, limit, log, fetcher, kind="posts"):
    return collect_json_list(site, limit=limit)
