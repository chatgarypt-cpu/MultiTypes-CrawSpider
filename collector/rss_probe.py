#!/usr/bin/env python3
"""RSS/HTML 站的补全覆盖探测（只读）。

借鉴成熟项目：
- Miniflux `internal/reader/subscription/finder.go` 的 well-known feed 路径与
  HTML <link rel="alternate"> 发现逻辑；
- Miniflux `internal/reader/fetcher/request_builder.go` 的条件请求（ETag/If-Modified-Since）；
- FreshRSS/feedparser 的增量轮询思路（本次只做探测，不建轮询）。
- Wayback CDX Server API 枚举"网上出现过的该站 URL"，用做覆盖率估算。

用法：
  python collector/rss_probe.py --sites-file /tmp/rss-sites.txt
输出：
  output/reports/rss-probe-<date>.csv（不含 data/ 写入）
"""
from __future__ import annotations

import argparse
import csv
import html as htmllib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml

from crawler.config import sources_path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
TIMEOUT = 20
# Miniflux finder.go 的 well-known 列表（另加几种常见变体）
WELL_KNOWN = ["/feed/", "/feed", "/rss/", "/rss", "/rss.xml", "/atom.xml", "/feed.xml",
              "/feed.atom", "/index.rss", "/index.xml", "/rss/feed.xml", "/?feed=rss2"]
CDX = "https://web.archive.org/cdx/search/cdx"


def get(url, timeout=TIMEOUT):
    try:
        return requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
    except Exception:
        return None


def find_feeds(base, body):
    """HTML autodiscovery + well-known 路径探测，返回去重后的 feed URL 列表。"""
    feeds = []
    for m in re.finditer(r'<link[^>]+(?:type=["\']application/(?:rss|atom)\+xml["\'][^>]*|'
                         r'rel=["\'][^"\']*alternate[^>]*)[^>]*>', body or "", re.I):
        tag = m.group(0)
        href = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
        typ = re.search(r'type=["\']([^"\']+)["\']', tag, re.I)
        if href and (not typ or "rss" in typ.group(1).lower() or "atom" in typ.group(1).lower()):
            feeds.append(urljoin(base, htmllib.unescape(href.group(1))))
    # 只对 base 做 well-known 探测（不猜子目录，避免大量 404）
    root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    for path in WELL_KNOWN:
        feeds.append(urljoin(root, path))
    # 去重保序
    seen, out = set(), []
    for f in feeds:
        if f not in seen:
            seen.add(f); out.append(f)
    return out


def parse_items(body):
    """极简 RSS/Atom 条目解析：返回 (count, [link,...])。"""
    items = re.findall(r"<(?:item|entry)[\s>].*?</(?:item|entry)>", body or "", re.S | re.I)
    links = []
    for block in items:
        m = re.search(r"<link[^>]*>([^<]+)</link>", block, re.I) or \
            re.search(r'<link[^>]*href=["\']([^"\']+)["\']', block, re.I)
        if m:
            links.append(htmllib.unescape(m.group(1).strip()))
    return len(items), links


def probe_feed(feed_url, max_pages=8):
    """探测 feed 是否可解析，并尝试 ?paged=N 翻页拿更多历史条目。"""
    r = get(feed_url)
    if r is None or r.status_code != 200:
        return None
    count, links = parse_items(r.text)
    if count == 0:
        return None
    paged = 1
    all_links = set(links)
    sep = "&" if "?" in feed_url else "?"
    for page in range(2, max_pages + 1):
        rr = get(f"{feed_url}{sep}paged={page}")
        if rr is None or rr.status_code != 200:
            break
        c, ls = parse_items(rr.text)
        if c == 0:
            break
        before = len(all_links)
        all_links.update(ls)
        if len(all_links) == before:
            break
        paged = page
    return {"url": feed_url, "items": count, "paged_pages": paged, "unique_links": len(all_links)}


def is_article_url(u):
    """粗过滤 CDX 里的噪声 URL（搜索引擎残留、资源、分类页、查询串）。"""
    from urllib.parse import unquote
    pr = urlparse(u)
    path = unquote(pr.path)
    if pr.query or not path.strip("/"):
        return False
    low = path.lower()
    if re.search(r"/(wp-json|wp-content|wp-includes|feed|rss|tag|category|author|search|page|comment|login|admin|\.well-known)", low):
        return False
    last = [s for s in path.split("/") if s][-1]
    if not re.search(r"[a-zA-Z]{3,}", last):
        return False
    junk = ("&", "=", "target=", "onmouse", "ved=", "sa=u", "usg=", "%20", "ipc:", "y=ipc")
    return not any(j in last.lower() for j in junk)


def local_urls(domain):
    p = ROOT.parent / "data" / domain / "urls.txt"
    try:
        return [ln.strip() for ln in open(p, encoding="utf-8", errors="ignore") if ln.strip()]
    except OSError:
        return []


def cdx_original_urls(domain, limit=5000):
    """Wayback CDX：枚举该域名下过 200 的 HTML URL（collapse=urlkey 去重）。"""
    params = {"url": f"{domain}/*", "output": "json", "collapse": "urlkey",
              "filter": "statuscode:200", "filter": "mimetype:text/html",
              "fl": "original", "limit": str(limit)}
    try:
        r = requests.get(CDX, params=params, headers=UA, timeout=60)
        if r.status_code != 200:
            return None
        rows = r.json()
        return [row[0] for row in rows[1:]] if rows and len(rows) > 1 else []
    except Exception:
        return None


def probe_site(site):
    dom, base = site["domain"], site["base"].rstrip("/")
    home = get(base)
    body = home.text if home is not None and home.status_code == 200 else ""
    feeds = find_feeds(base, body)
    results = []
    for f in feeds[:14]:
        res = probe_feed(f)
        if res:
            results.append(res)
    # 用 paged 最多的一个 feed 的链接集合做本地覆盖率对比
    best = max(results, key=lambda r: r["unique_links"]) if results else None
    loc = local_urls(dom)
    cdx_raw = cdx_original_urls(dom)
    cdx = [u for u in (cdx_raw or []) if is_article_url(u)] if cdx_raw is not None else None
    norm = lambda u: re.sub(r"^https?://(www\.)?", "", u.strip().lower()).rstrip("/")
    loc_set = {norm(u) for u in loc}
    missing = []
    if cdx:
        cd_set = {norm(u) for u in cdx}
        missing = sorted(cd_set - loc_set)[:3]
    return {"domain": dom, "base": base, "home": home.status_code if home is not None else None,
            "feeds_found": len(feeds), "feeds_ok": len(results),
            "feed_items": best["items"] if best else 0,
            "paged_pages": best["paged_pages"] if best else 0,
            "feed_unique_links": best["unique_links"] if best else 0,
            "local_urls": len(loc), "cdx_raw": len(cdx_raw) if cdx_raw is not None else None,
            "cdx_urls": len(cdx) if cdx is not None else None,
            "est_cov_by_cdx": round(len(loc) / len(cdx), 3) if cdx else None,
            "feed_sample": best["url"] if best else "",
            "missing_sample": " | ".join(missing)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites-file", required=True, help="每行一个 domain（# 注释）")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    sites = {s["domain"]: s for s in yaml.safe_load(open(sources_path(ROOT), encoding="utf-8")) if s.get("domain")}
    doms = [ln.split("#")[0].strip() for ln in open(a.sites_file, encoding="utf-8") if ln.split("#")[0].strip()]
    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for row in ex.map(lambda d: probe_site(sites[d]), [d for d in doms if d in sites]):
            rows.append(row)
            print(f"{row['domain']:30} feeds={row['feeds_ok']}/{row['feeds_found']} "
                  f"items={row['feed_items']:5} paged={row['paged_pages']} "
                  f"links={row['feed_unique_links']:5} local={row['local_urls']:6} "
                  f"cdx={row['cdx_urls']} cov={row['est_cov_by_cdx']}", flush=True)
    out = ROOT.parent / "output" / "reports" / f"rss-probe-{date.today().strftime('%Y%m%d')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["domain", "base", "home", "feeds_found", "feeds_ok", "feed_items", "paged_pages",
            "feed_unique_links", "local_urls", "cdx_raw", "cdx_urls", "est_cov_by_cdx", "feed_sample",
            "missing_sample"]
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows: w.writerow({k: r.get(k) for k in cols})
    print("\nreport ->", out)


if __name__ == "__main__":
    main()
