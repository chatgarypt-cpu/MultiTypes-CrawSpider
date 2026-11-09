"""Shared helpers."""
import html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests, trafilatura
from lxml import html as lxml_html
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

def _page_urls(site):
    """列表页地址（支持 {page} / {offset} 占位符，page_step 为步长）。"""
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
    return pages


ASSET = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|json|xml|pdf|zip|mp4|mp3|woff2?|ttf|eot)$", re.I)
SKIP_SCHEME = ("#", "javascript:", "mailto:", "tel:")


def html_urls(site, fetcher, limit):
    """列表页 → 文章 URL。

    - `item_selector`：可选 XPath，返回 href 字符串（如 //a[contains(@href,'/news/')]/@href）；
    - `link_pattern`：对解析后的绝对 URL 过滤（既有正则都没有 ^ 锚点，语义不变）；
    - 相对链接用 urljoin 按所在列表页解析（旧实现用 base + "/" 拼，子目录站点会拼错）；
    - 资源/锚点/javascript 链接一律丢弃（旧实现会捡到 /media/ 下的 .css）。
    """
    pat = site.get("link_pattern")
    sel = site.get("item_selector")
    out, seen = [], set()
    pages = _page_urls(site)
    if getattr(fetcher, "ego", False) or getattr(fetcher, "browser", False) or getattr(fetcher, "cloud", False):
        results = fetcher.get_many(pages)
    else:
        results = list(fetcher.stream_many(pages, workers=4))
    host = urlparse(site["base"]).netloc
    for page_url, status, body in results:
        if status != 200 or not body:
            continue
        try:
            doc = lxml_html.fromstring(body)
        except Exception:
            continue
        hrefs = []
        if sel:                                   # 显式选择器：只信它，不再叠加启发式
            try:
                hrefs = [x for x in doc.xpath(sel) if isinstance(x, str)]
            except Exception:
                hrefs = []
        else:
            hrefs = [a.get("href") for a in doc.xpath("//a[@href]")]
        for href in hrefs:
            href = (href or "").strip()
            if not href or href.startswith(SKIP_SCHEME) or ASSET.search(href):
                continue
            full = urljoin(page_url, href)        # 先按列表页解析相对链接，再按绝对 URL 过滤
            if pat and not re.search(pat, full):
                continue
            if not pat and not sel and not re.search(r"/20\d\d/|/news/|/article|/story|/press|/media", full):
                continue
            if full not in seen and (host in full or site["domain"] in full):
                seen.add(full); out.append(full)
    return out[:limit] if limit else out


def iter_sitemap_urls(site, fetcher, cap=0, chunk_children=5, failures=None):
    """Yield sitemap URLs incrementally so a 400k-URL sitemap never sits in memory.

    failures: 可选 list；非 200 的 sitemap 入口（含子图）会 append {"url","status"}，
    由 channels/base 决定"可重试"还是"已删除"，避免一个子图 503 时整站被标 complete。
    """
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
                if failures is not None:
                    failures.append({"url": u, "status": status})
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
