#!/usr/bin/env python3
"""CLI for the modular crawler.

  uv run --with requests --with pyyaml --with trafilatura python3 collector/collect.py --site <domain> --limit 50
  ... --all --limit 20
  ... --validate                    # sources.yaml 校验（含重复 key 检测）
  ... --check-site <domain>         # 干跑：只跑发现阶段，打印条数 + 前 3 条 URL，不入库

Channels live in collector/crawler/channels/ and are selected by `channel:` in sources.yaml.
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml                                              # noqa: E402

from crawler.config import ROOT, DATA                   # noqa: E402
from crawler.runner import collect_site                  # noqa: E402
from crawler.store import load_cursor, append_rows, load_known, save_cursor  # noqa: E402  (legacy imports)
from crawler.mapping import map_wp_post, map_wp_media, map_json_item, _flatten_rich  # noqa: E402
from crawler.extract import extract_article              # noqa: E402
from crawler.discover import sitemap_urls, rss_urls, html_urls  # noqa: E402
from crawler.web import Fetcher, UA                              # noqa: E402
import json as _json, requests as _requests                      # noqa: E402
UA = UA
requests = _requests
json = _json


def check_site(site, cap=None):
    """Dry-run discovery only: fetch listing/sitemap/feed, return count + first URLs.

    只跑发现阶段（discover）：不写 cursor、不入库、不抓正文。
    返回 {"domain", "channel", "discovered", "sample", ...}。
    """
    from crawler.discover import iter_sitemap_urls, wp_page_url
    from crawler.web import headers_for, parse_json_tolerant, _get

    if cap is None:
        cap = int(site.get("discover_cap") or os.environ.get("CHECK_SITE_CAP", "200"))
    site = dict(site, via=site.get("via_posts") or site.get("via"))
    domain, channel = site.get("domain"), site.get("channel")
    fetcher = Fetcher(site)
    result = {"domain": domain, "channel": channel, "discovered": 0, "sample": []}
    if channel == "sitemap":
        urls = list(iter_sitemap_urls(site, fetcher, cap))
    elif channel == "rss":
        urls = rss_urls(site, fetcher, cap)
    elif channel == "html":
        urls = html_urls(site, fetcher, cap)
    elif channel == "wp-api":
        st, body, hdr = _get(None, wp_page_url(site, 1), timeout=60, headers=headers_for(site))
        if st != 200:
            return dict(result, error=f"first page {st} {str(body)[:120]}")
        posts = parse_json_tolerant(body) or []
        urls = [p.get("link") for p in posts if isinstance(p, dict)]
        total = hdr.get("x-wp-total")
        result["pages"] = hdr.get("x-wp-totalpages")
        if str(total or "").isdigit():
            result["discovered"] = int(total)
            result["sample"] = [u for u in urls if u][:3]
            return result
    elif channel == "json-api":
        base = site["base"].rstrip("/")
        url = f"{base}{site.get('api') or ''}?{site.get('api_query') or 'limit=100&meta=total_count'}"
        st, body, _hdr = _get(None, url, timeout=60, headers=headers_for(site))
        data = parse_json_tolerant(body) if st == 200 else None
        if not isinstance(data, dict):
            return dict(result, error=f"first page {st}")
        items = data.get("data") or []
        result["discovered"] = int((data.get("meta") or {}).get("total_count") or len(items))
        return result
    elif channel == "json-list":
        base = site["base"].rstrip("/")
        cats = site.get("api_categories") or [None]
        pat = site.get("api_list") or ""
        url = pat.format(page=1, category=cats[0]) if cats[0] else pat.format(page=1)
        url = url if url.startswith("http") else base + url
        st, body, _hdr = _get(None, url, timeout=60, headers=headers_for(site))
        data = parse_json_tolerant(body) if st == 200 else None
        if not isinstance(data, dict):
            return dict(result, error=f"first page {st}")
        result["discovered"] = len(data.get(site.get("api_items") or "stories") or data.get("data") or [])
        return result
    else:
        return dict(result, error=f"check-site unsupported for channel {channel!r}")
    urls = [u for u in urls if u]
    result["discovered"] = len(urls)
    result["sample"] = urls[:3]
    if cap and len(urls) >= cap:
        result["capped"] = cap
    return result


def print_check_site(result):
    print(f"check-site {result['domain']} [{result['channel']}] discovered {result['discovered']} URL(s)"
          + (f" (capped at {result['capped']})" if result.get("capped") else ""))
    for u in result.get("sample") or []:
        print(f"  {u}")
    if result.get("error"):
        print(f"  error: {result['error']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--type")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--direct-wp", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--check-site", metavar="DOMAIN", help="dry-run discovery only: print count + first 3 URLs")
    ap.add_argument("--url", help="crawl one article URL (no need to add it to sources.yaml)")
    ap.add_argument("--site-url", help="point at a site homepage/listing and auto-detect how to crawl it")
    ap.add_argument("--detect", action="store_true", help="only print the detected rule for --site-url")
    ap.add_argument("--kind", default="posts", choices=["posts", "pages", "media"])
    a = ap.parse_args()

    if a.url:
        from crawler.adhoc import collect_one
        print(json.dumps(collect_one(a.url), ensure_ascii=False))
        return

    if a.site_url:
        from crawler.adhoc import detect, remember
        site = detect(a.site_url)
        remember(site)
        if a.detect or a.limit == 0:
            print(json.dumps(site, ensure_ascii=False, indent=1))
            return
        import glob
        for cp in glob.glob(os.path.join(DATA, site["domain"], "*cursor*.json")):
            os.remove(cp)                       # a manual run always re-discovers
        print(json.dumps({"detected": site}, ensure_ascii=False), flush=True)
        print(json.dumps(collect_site(site, a.limit, a.kind), ensure_ascii=False), flush=True)
        return

    sources_path = os.path.join(ROOT, "sources.yaml")
    if a.validate:
        from crawler.schema import validate_file
        errs, warns, sites = validate_file(sources_path)
        for w in warns:
            print("WARN ", w)
        for e in errs:
            print("ERROR", e)
        print(f"\n{len(sites)} sites, {len(errs)} errors, {len(warns)} warnings")
        sys.exit(1 if errs else 0)

    sites = yaml.safe_load(open(sources_path))
    if a.check_site:
        site = next((s for s in sites if s.get("domain") == a.check_site), None)
        if site is None:
            sys.exit(f"check-site: domain {a.check_site!r} not found in sources.yaml")
        result = check_site(site)
        print_check_site(result)
        if result.get("error"):
            sys.exit(1)
        return

    if a.site:
        sites = [s for s in sites if s["domain"] == a.site]
    elif a.direct_wp:
        sites = [s for s in sites if s["channel"] == "wp-api" and s.get("via", "direct") != "browser"]
        sites.sort(key=lambda s: (load_cursor(s["domain"]).get("total_posts") or 0))
    elif a.type:
        sites = [s for s in sites if s.get("type") == a.type]
    elif not a.all:
        sys.exit("need --site or --all")

    for s in sites:
        if a.reset:
            cp = os.path.join(DATA, s["domain"], "cursor.json")
            if os.path.exists(cp):
                os.remove(cp)
        try:
            r = collect_site(s, a.limit, a.kind)
            print(json.dumps(r, ensure_ascii=False), flush=True)
        except Exception as e:
            print(json.dumps({"domain": s["domain"], "error": repr(e)[:160]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
