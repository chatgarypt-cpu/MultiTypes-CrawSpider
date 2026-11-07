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
from crawler.extract import extract_article
from crawler.web import Fetcher, _get, wp_json, cloud_get
from crawler.pipeline import normalize, validate

def collect_json(site, limit=0, workers=8, progress=True, kind="posts"):
    """Generic JSON API channel (Directus-style: {data: [...], meta: {total_count}})."""
    base = site["base"].rstrip("/")
    api = site.get("api") or ""
    query = site.get("api_query") or "limit=100&meta=total_count"
    known = load_known(site["domain"], "posts")
    log = logging.getLogger(site["domain"])
    prefix_map = None
    cat_cfg = site.get("api_category") or {}
    if cat_cfg.get("api"):
        try:
            cr = requests.get(base + cat_cfg["api"] + "?" + (cat_cfg.get("query") or ""),
                              headers={"User-Agent": UA}, timeout=45)
            cats_rows = (cr.json() or {}).get("data") or []
            by_id = {c["id"]: c for c in cats_rows}
            def prefix_map(cid, _by=by_id):
                c = _by.get(cid)
                if not c: return ""
                par = c.get("parent")
                if isinstance(par, int) and par in _by:
                    return f"{_by[par]['name']}/{c['name']}"
                if isinstance(par, dict):
                    return f"{par.get('name')}/{c['name']}"
                return c["name"]
        except Exception as e:
            log.warning("%s category map failed: %s", site["domain"], str(e)[:60])
    saved, page, total = 0, 1, None
    t0 = time.time()
    while True:
        u = f"{base}{api}?{query}" + (f"&page={page}" if "page=" not in query else "")
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=60)
            d = r.json()
        except Exception as e:
            log.warning("%s json page %s failed: %s", site["domain"], page, str(e)[:80]); break
        items = d.get("data") or []
        if total is None:
            total = (d.get("meta") or {}).get("total_count")
        if not items: break
        rows = []
        for it in items:
            if "status" in it and it["status"] != "published": continue
            row = map_json_item(it, site, prefix_map)
            if not row["url"] or row["url"] in known: continue
            row = normalize(row, site)
            ok, _why = validate(row, site, kind)
            if not ok: continue
            known.add(row["url"]); rows.append(row)
        if rows:
            written = append_rows(site["domain"], rows, "posts")
            saved += written if isinstance(written, int) else len(rows)
            if progress: log.info("%s json page %s saved=%d total=%s", site["domain"], page, saved, total)
        if limit and saved >= limit: break
        if total and page >= math.ceil(total / int(site.get("api_page_size", 100))) + 1: break
        page += 1
        if page > 500: break
    return {"domain": site["domain"], "channel": "json-api", "via": "direct", "saved": saved,
            "total": total, "pages": page - 1, "secs": round(time.time() - t0, 1)}

def run(site, limit, log, fetcher, kind="posts"):
    return collect_json(site, limit=limit, kind=kind)
