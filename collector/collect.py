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

from crawler.config import ROOT, DATA, sources_path     # noqa: E402
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


def check_site(site, cap=None, samples=3):
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
            result["sample"] = [u for u in urls if u][:samples]
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
    result["sample"] = urls[:samples]
    if cap and len(urls) >= cap:
        result["capped"] = cap
    return result


def check_extract(site, samples=8):
    """干跑：发现阶段取样本 URL，逐条抓取+抽取，报告字段是否可用（不写库、不写游标）。

    给"Agent 生成/修改站点规则"用：先 check-site 看发现条数，再 check-extract 看正文/
    标题/日期/schema 是否稳定，验证过再写进 sources.yaml 批量跑。
    """
    from crawler import htmlpage
    from crawler.extract import extract_best
    from crawler.web import Fetcher

    site = dict(site, via=site.get("via_posts") or site.get("via"))
    discovered = check_site(site, samples=samples)
    urls = [u for u in (discovered.get("sample") or []) if u]
    out = {"domain": site.get("domain"), "channel": site.get("channel"),
           "discovered": discovered.get("discovered"), "error": discovered.get("error"),
           "checked": 0, "ok": 0, "rows": []}
    if not urls:
        return out
    fetcher = Fetcher(site)
    for u, st, body in fetcher.stream_many(urls, workers=1):
        row = extract_best(u, body, site, min_text=0) if st == 200 else None
        rec = {"url": u, "http": st, "text_len": 0, "title": "", "created_at": "",
               "author": "", "extractor": "", "schema_title_ok": None}
        if row:
            rec.update(text_len=len(row.get("text") or ""),
                       title=(row.get("title") or "")[:100],
                       created_at=(row.get("created_at") or "")[:19],
                       author=(row.get("user_author_name") or "")[:40],
                       extractor=row.get("_extractor", ""))
            schema = (site.get("schema") or {}).get("title")
            if schema:
                h1 = htmlpage.xpath_value(body, "//h1/text()")
                rec["schema_title_ok"] = bool(h1) and rec["title"] == h1
            out["checked"] += 1
            out["ok"] += 1
        out["rows"].append(rec)
    return out


def print_check_extract(result):
    print(f"check-extract {result['domain']} [{result['channel']}] discovered {result['discovered']}"
          f"  checked {result['checked']}  ok {result['ok']}")
    if result.get("error"):
        print(f"  error: {result['error']}")
    for r in result["rows"]:
        print(f"  http={r['http']} text={r['text_len']:5} schema_ok={r['schema_title_ok']} "
              f"ext={r['extractor'] or '-':10} date={r['created_at'] or '-':19} "
              f"title={r['title'][:60]!r}")


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
    ap.add_argument("--no-csv", action="store_true", help="跳过本轮 CSV 导出")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--type")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--direct-wp", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--check-site", metavar="DOMAIN", help="dry-run discovery only: print count + first 3 URLs")
    ap.add_argument("--check-extract", metavar="DOMAIN", help="dry-run discovery + fetch/extract samples (no writes)")
    ap.add_argument("--check-types", metavar="DOMAIN", help="inspect WordPress REST types and report unconfigured collections")
    ap.add_argument("--samples", type=int, default=8, help="--check-extract 抽样条数（默认 8）")
    ap.add_argument("--url", help="crawl one article URL (no need to add it to sources.yaml)")
    ap.add_argument("--site-url", help="point at a site homepage/listing and auto-detect how to crawl it")
    ap.add_argument("--detect", action="store_true", help="only print the detected rule for --site-url")
    ap.add_argument("--kind", default="posts", help="posts/pages/media or a configured wp_types collection")
    a = ap.parse_args()
    import re
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", a.kind):
        ap.error("invalid collection name")
    from crawler.channels import wp_api
    wp_api.WORKERS = max(1, a.workers)

    if a.url:
        from crawler.adhoc import collect_one
        result = collect_one(a.url)
        print(json.dumps(result, ensure_ascii=False))
        return int(bool(result.get("error")))

    if a.site_url:
        from crawler.adhoc import detect, remember
        site = detect(a.site_url)
        remember(site)
        if a.detect:
            print(json.dumps(site, ensure_ascii=False, indent=1))
            return
        print(json.dumps({"detected": site}, ensure_ascii=False), flush=True)
        result = collect_site(site, a.limit, a.kind, export_csv=not a.no_csv, reset=a.reset)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return int(bool(result.get("error") or result.get("export_error")))

    catalog = sources_path(ROOT)
    if a.validate:
        from crawler.schema import validate_file
        errs, warns, sites = validate_file(catalog)
        for w in warns:
            print("WARN ", w)
        for e in errs:
            print("ERROR", e)
        print(f"\n{len(sites)} sites, {len(errs)} errors, {len(warns)} warnings")
        sys.exit(1 if errs else 0)

    from crawler.schema import find_duplicate_keys
    with open(catalog, encoding="utf-8") as f:
        sources_text = f.read()
    duplicate_errors = find_duplicate_keys(sources_text, str(catalog))
    if duplicate_errors:
        ap.error("\n".join(duplicate_errors))
    sites = yaml.safe_load(sources_text)
    if a.check_types:
        from crawler.discover import wp_type_inventory
        site = next((s for s in sites if s.get("domain") == a.check_types), None)
        if not site or site.get("channel") != "wp-api":
            ap.error("--check-types requires a configured wp-api site")
        result = wp_type_inventory(site, Fetcher(site))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(1 if result.get("error") else 0)
    if a.check_site:
        site = next((s for s in sites if s.get("domain") == a.check_site), None)
        if site is None:
            sys.exit(f"check-site: domain {a.check_site!r} not found in sources.yaml")
        result = check_site(site)
        print_check_site(result)
        if result.get("error"):
            sys.exit(1)
        return

    if a.check_extract:
        site = next((s for s in sites if s.get("domain") == a.check_extract), None)
        if site is None:
            sys.exit(f"check-extract: domain {a.check_extract!r} not found in sources.yaml")
        result = check_extract(site, a.samples)
        print_check_extract(result)
        if result.get("error"):
            sys.exit(1)
        return

    if a.site:
        sites = [s for s in sites if s["domain"] == a.site]
        if not sites:
            ap.error(f"{a.site}: site not found in sources.yaml")
    elif a.direct_wp:
        sites = [s for s in sites if s["channel"] == "wp-api" and s.get("via", "direct") != "browser"]
        sites.sort(key=lambda s: (load_cursor(s["domain"]).get("total_posts") or 0))
    elif a.type:
        sites = [s for s in sites if s.get("type") == a.type]
    elif not a.all:
        sys.exit("need --site or --all")

    # 人工标记 disabled 的站不参与批量/类型采集；显式 --site 仍可单独跑（便于临时验证）
    if not a.site:
        skipped_disabled = [s["domain"] for s in sites if s.get("disabled")]
        sites = [s for s in sites if not s.get("disabled")]
        if skipped_disabled:
            print(f"skip {len(skipped_disabled)} disabled site(s): "
                  + ", ".join(skipped_disabled), flush=True)

    failed = False
    for s in sites:
        from crawler.config import site_kinds
        if a.kind not in site_kinds(s):
            if a.site:
                ap.error(f"{s['domain']}: collection {a.kind!r} is not configured")
            continue
        try:
            r = collect_site(s, a.limit, a.kind, export_csv=not a.no_csv, reset=a.reset)
            print(json.dumps(r, ensure_ascii=False), flush=True)
            failed |= bool(r.get("error") or r.get("export_error"))
        except Exception as e:
            failed = True
            print(json.dumps({"domain": s["domain"], "error": repr(e)[:160]}, ensure_ascii=False), flush=True)
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
