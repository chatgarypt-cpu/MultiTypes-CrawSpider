"""Shared helpers."""
import html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests, trafilatura
from crawler.config import ROOT, DATA, DB, UA, COLS, MEDIA_COLS, WORKERS

def wp_page_url(site, page, per=50, kind="posts"):
    per = int(site.get("wp_per_page") or per)
    """REST URL for one page of a collection; per-site ordering override supported."""
    ep = kind
    embed = "" if (kind == "media" or site.get("no_embed")) else "&_embed=1"
    order = site.get("orderby", "id")
    tail = f"&orderby={order}&order={site.get('order', 'asc')}" if order else ""
    # `_fields` 是为 posts 配的精简字段集，套到 media 上会把 source_url/mime_type 等滤掉，
    # 导致 media 行 url 为空、全部被丢弃；media 用独立字段表，未配置就不过滤。
    fields = site.get("wp_fields_media") if kind == "media" else site.get("wp_fields")
    if fields:
        tail += "&_fields=" + fields
    if site.get("api") == "rest_route":
        return f"{site['base']}/?rest_route=/wp/v2/{ep}&per_page={per}&page={page}{embed}{tail}"
    return f"{site['base']}/wp-json/wp/v2/{ep}?per_page={per}&page={page}{embed}{tail}"


def sitemap_urls(site, fetcher, limit):
    base = site["base"]; sm = site.get("sitemap", "/sitemap.xml")
    got, seen = [], set()
    queue = [base + sm]
    while queue and (not limit or len(got) < limit * 3):
        batch = queue[:5]; queue = queue[5:]
        for u, status, body in fetcher.get_many(batch):
            if status != 200: continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
            if re.search(r"<sitemapindex", body, re.I):
                queue += [l for l in locs if l not in seen][:80]
            else:
                pat = site.get("url_filter")
                for l in locs:
                    if l in seen or re.search(r"\.(jpg|jpeg|png|gif|css|js|pdf|xml)$", l, re.I): continue
                    if pat and not re.search(pat, l): continue
                    seen.add(l); got.append(l)
            seen.update(locs)
    return got[:limit] if limit else got

def rss_urls(site, fetcher, limit):
    """RSS/Atom 条目 URL。feed 可带 {start} 或 {page} 占位符做翻页（配合 page_step/pages）。"""
    tpl = site.get("feed", "/feed/")
    count = int(site.get("pages", 1) or 1)
    if "{start}" in tpl:
        step = int(site.get("page_step", 10))
        feeds = [site["base"] + tpl.replace("{start}", str(i * step)) for i in range(count)]
    elif "{page}" in tpl:
        step = int(site.get("page_step", 1))
        feeds = [site["base"] + tpl.replace("{page}", str(1 + i * step)) for i in range(count)]
    else:
        feeds = [site["base"] + tpl]
    out = []
    for u, status, body in fetcher.get_many(feeds):
        if status != 200:
            continue
        for block in re.findall(r"<(?:item|entry)[\s>].*?</(?:item|entry)>", body, re.S | re.I):
            m = re.search(r"<link[^>]*>([^<]+)</link>", block) or re.search(r'<link[^>]*href="([^"]+)"', block)
            if m: out.append(html.unescape(m.group(1).strip()))
    return out[:limit] if limit else out if limit else out

def html_urls(site, fetcher, limit):
    pat = site.get("link_pattern")
    out, seen = [], set()
    raw = site.get("list_urls") or [site.get("list_url") or (site["base"] + "/")]
    step = int(site.get("page_step", 10)); count = int(site.get("pages", 1))
    pages = []
    for u in raw:
        if u.startswith("/"):
            u = site["base"].rstrip("/") + u
        if "{offset}" in u:                       # e.g. /Actualites/(offset)/{offset}
            pages += [u.replace("{offset}", str(i * step)) for i in range(count)]
        elif "{page}" in u:                       # e.g. /actualites?page={page}
            steps = int(site.get("page_step", 1))
            pages += [u.replace("{page}", str(i * steps)) for i in range(count)]
        else:
            pages.append(u)
    # 列表页很多时（如 wallis 60 个分页）串行抓会非常慢：本机网络出口可并行就用 stream_many
    if getattr(fetcher, "ego", False) or getattr(fetcher, "browser", False) or getattr(fetcher, "cloud", False):
        results = fetcher.get_many(pages)
    else:
        results = list(fetcher.stream_many(pages, workers=4))
    for u, status, body in results:
        if status != 200: continue
        for href in re.findall(r'href="([^"#]+)"', body):
            if pat and not re.search(pat, href): continue
            if not pat and not re.search(r"/20\d\d/|/news/|/article|/story|/press|/media", href): continue
            full = href if href.startswith("http") else site["base"] + "/" + href.lstrip("/")
            host = urlparse(site["base"]).netloc
            if full not in seen and (host in full or site["domain"] in full):
                seen.add(full); out.append(full)
    return out[:limit] if limit else out if limit else out


def iter_sitemap_urls(site, fetcher, cap=0, chunk_children=5):
    """Yield sitemap URLs incrementally so a 400k-URL sitemap never sits in memory."""
    base = site["base"]
    sm = site.get("sitemap", "/sitemap.xml")
    seen, queue = set(), [base + sm]
    filt = site.get("url_filter")
    pat = site.get("link_pattern")
    yielded = 0
    while queue:
        batch, queue = queue[:chunk_children], queue[chunk_children:]
        for u, status, body in fetcher.get_many(batch):
            if status != 200:
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
            if re.search(r"<sitemapindex", body, re.I):
                queue += [l for l in locs if l not in seen][:80]
                seen.update(locs)
                continue
            for l in locs:
                if re.search(r"\.(jpg|jpeg|png|gif|css|js|pdf|xml)$", l, re.I):
                    continue
                if filt and not re.search(filt, l):
                    continue
                if pat and not re.search(pat, l):
                    continue
                yield l
                yielded += 1
                if cap and yielded >= cap:
                    return
