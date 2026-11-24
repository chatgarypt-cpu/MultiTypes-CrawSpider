#!/usr/bin/env python3
"""Wayback CDX → 采集队列：给"原站已下线 / 全站 403"的站点补历史 URL。

这类站点（如 tonga-broadcasting.net 已下线、tokelau.org.nz 被 CloudFront 按 IP 拦）
发现阶段拿不到任何链接，`paywall_fallback` 再强也没有 URL 可抓。CDX 接口能给出
archive.org 上真实存在的 HTML 页面清单，直接写进 RequestQueue（channel='cdx'），
随后 `collect.py --site <domain>` 会把这些 URL 逐条交给 paywall 回退去取正文。

  python collector/cdx_seed.py --domain www.tonga-broadcasting.net --limit 2000
  python collector/cdx_seed.py --domain www.tokelau.org.nz --pages-only --dry-run

只入队，不抓取；不会把 URL 标成 done，也不会写 articles.jsonl。
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crawler.request import RequestQueue, canonical_key   # noqa: E402
from crawler.store import load_known                     # noqa: E402

CDX_API = "https://web.archive.org/cdx/search/cdx"
ASSET_EXT = re.compile(r"\.(jpe?g|png|gif|css|js|ico|zip|gz|tgz|rar|7z|svg|webp|woff2?|ttf|eot|mp4|mp3|avi|mov|docx?|xlsx?|pptx?)$", re.I)
JUNK = re.compile(r"(^|/)(\.[a-z0-9_-]+/?$|wp-(admin|login|json|content/(plugins|themes))|feed|rss|xmlrpc|sitemap)", re.I)
BOILERPLATE = re.compile(r"/blank\.html?$|/\d+(\.\d+)*\.html?$", re.I)   # 空壳页 / 版本页
HIDDEN = re.compile(r"(^|/)\.")
CLEAN_HTML = re.compile(r"\.html?$", re.I)     # 历史链接拼接的判据：含 .htm 但不在结尾（xxx.htmlInterview）


def cdx_urls(domain, limit=2000, since=None, pages_only=True, retries=3, timeout=90):
    """返回 archive.org 上该域的状态 200 HTML 快照 URL（已去重、去资源、去查询串）。"""
    params = {
        "url": f"{domain}/*", "output": "json", "limit": limit,
        "filter": "statuscode:200",
        "collapse": "urlkey", "fl": "original,timestamp",
    }
    if pages_only:
        params["filter"] = ["statuscode:200", "mimetype:text/html"]
    if since:
        params["from"] = since
    last = ""
    for attempt in range(retries):
        try:
            r = requests.get(CDX_API, params=params, timeout=timeout)
            r.raise_for_status()
            rows = r.json() or []
            break
        except Exception as exc:                 # 网络抖动/限流：退避后重试
            last = f"{type(exc).__name__}: {exc}"[:120]
            time.sleep(2 + attempt * 3)
    else:
        raise RuntimeError(f"CDX 查询失败：{last}")

    out, seen = [], set()
    for row in rows[1:]:
        url = row[0]
        path = urlsplitsafe_path(url)
        if "?" in url or "%" in url or ASSET_EXT.search(path):
            continue
        if JUNK.search(path) or BOILERPLATE.search(path) or HIDDEN.search(path):
            continue
        if ".htm" in path.lower() and not CLEAN_HTML.search(path):
            continue
        # 同一页面的 http/https、:80、末尾斜杠变体只留一条
        key = variant_key(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def variant_key(url):
    """协议/端口无关的页面标识，用来合并 CDX 里同一页的多条历史记录。"""
    p = urlsplit(url if "://" in url else "http://" + url)
    host = (p.netloc or "").lower().replace(":80", "").replace(":443", "")
    host = host[4:] if host.startswith("www.") else host
    return host + (p.path.rstrip("/") or "/")


def urlsplitsafe_path(url):
    try:
        return urlsplit(url if "://" in url else "http://" + url).path
    except Exception:
        return url


def seed(domain, urls, kind="posts", channel="cdx", limit=0, dry_run=False):
    """把 URL 入队；已在磁盘（articles.jsonl）里的自然被 seed_done 挡掉。"""
    known = {canonical_key(u) for u in load_known(domain, kind)}
    fresh = [u for u in urls if canonical_key(u) not in known]
    if limit:
        fresh = fresh[:limit]
    q = RequestQueue(domain, kind)
    added = 0 if dry_run else q.add_many(fresh, channel=channel)
    return {"domain": domain, "cdx": len(urls), "known": len(urls) - len(fresh),
            "queued": len(fresh), "added": added, "dry_run": dry_run}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wayback CDX → 采集队列（原站下线时的补采入口）")
    ap.add_argument("--domain", required=True)
    ap.add_argument("--kind", default="posts")
    ap.add_argument("--limit", type=int, default=2000, help="CDX 最多取多少条（默认 2000）")
    ap.add_argument("--queue-limit", type=int, default=0, help="最多入队多少条（0=全部）")
    ap.add_argument("--since", help="CDX from 参数，如 2020")
    ap.add_argument("--all", action="store_true", help="不限 mimetype（含 PDF 等）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    urls = cdx_urls(args.domain, limit=args.limit, since=args.since, pages_only=not args.all)
    res = seed(args.domain, urls, kind=args.kind, limit=args.queue_limit, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
