#!/usr/bin/env python3
"""按源站 REST 清单补采"有缺口"的 wp-api 站点：只抓本地 urls.txt 里没有的条目。

用法：
  python collector/backfill_gaps.py --site www.tntv.pf --site www.gov.to [--dry-run]
  python collector/backfill_gaps.py --site www.wpcouncil.org --kind media

只用于 wp-api 通道（源站能给完整清单）；有缺口时先跑这个，再跑 verify_coverage 复核。
不会重抓已入库 URL（urls.txt 去重）；被 validate 丢弃的短内容会计入 reasons，说明是
"源站口径含短内容"而不是漏采。
"""
import argparse
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import yaml  # noqa: E402

from crawler.mapping import map_wp_media, map_wp_post  # noqa: E402
from crawler.pipeline import normalize, validate  # noqa: E402
from crawler.store import append_rows, load_known  # noqa: E402
from crawler.web import _get, headers_for, parse_json_tolerant  # noqa: E402

PER = 100
MAX_PAGES = 2000
FIELDS = {
    "posts": "id,date,date_gmt,modified,modified_gmt,link,title,content,excerpt,author,featured_media,categories,jetpack_featured_media_url",
    "pages": "id,date,date_gmt,modified,modified_gmt,link,title,content,author,featured_media",
    "media": "id,title,source_url,link,mime_type,media_details,alt_text,caption,description,date_gmt,post",
}


def list_url(site, kind, page, per=PER):
    """带 _fields 的列表 URL：backfill 只需要映射字段，避免 _embed/全量 payload 拖慢整站扫描。"""
    base = site["base"].rstrip("/")
    fields = FIELDS.get(kind, "id,link")
    if site.get("api") == "rest_route":
        return (f"{base}/?rest_route=/wp/v2/{kind}&per_page={per}&page={page}"
                f"&_fields={fields}")
    return f"{base}/wp-json/wp/v2/{kind}?per_page={per}&page={page}&_fields={fields}"


def fetch_pages(site, kind, log):
    """逐页拉取 REST 列表，yield 每一页的条目（自动使用 rest_route / no_embed 配置）。

    注意：不能用"本页不足 per_page 条"判断结束——fbcnews 的 media 第 1 页只有 91 条、
    但 X-WP-TotalPages=1925。优先用响应头 X-WP-TotalPages 判断末页。
    """
    total_pages = None
    for page in range(1, MAX_PAGES + 1):
        url = list_url(site, kind, page)
        st, body, hdr = _get(None, url, timeout=90, headers=headers_for(site))
        if st != 200:
            log(f"  page {page} http={st} -> stop")
            break
        data = parse_json_tolerant(body)
        if not isinstance(data, list):
            log(f"  page {page} non-list -> stop")
            break
        hdr = hdr or {}
        tp = hdr.get("X-WP-TotalPages") or hdr.get("x-wp-totalpages")
        if tp and str(tp).isdigit():
            total_pages = int(tp)
        yield page, data
        if total_pages:
            if page >= total_pages:
                break
        elif len(data) == 0:
            break


def backfill(site, kind, dry_run=False, html_fallback=False):
    dom = site["domain"]
    from crawler.request import canonical_key
    known = {canonical_key(u) for u in load_known(dom, kind)}
    log = lambda msg: print(msg, flush=True)
    log(f"== {dom} {kind}: 本地 {len(known)} 条，开始扫描源站")
    missing = []
    seen = set()
    for page, items in fetch_pages(site, kind, log):
        for it in items:
            if not isinstance(it, dict):
                continue
            # posts 用 link；media 的本地去重键是 source_url（见 mapping.map_wp_media.url）
            link = (it.get("source_url") if kind == "media" else it.get("link")) or ""
            key = canonical_key(link)
            if not link or key in known or key in seen:
                continue
            seen.add(key)
            missing.append(it)
        log(f"  page {page}: items={len(items)} missing_total={len(missing)}")
    log(f"  源站清单扫描完成：缺 {len(missing)} 条")
    if dry_run or not missing:
        return {"domain": dom, "kind": kind, "missing": len(missing), "saved": 0,
                "reasons": {}, "dry_run": dry_run}

    reasons = Counter()
    rows = []
    fallback_urls = []
    for it in missing:
        row = map_wp_media(it, site) if kind == "media" else map_wp_post(it, site)
        row = normalize(row, site)
        ok, why = validate(row, site, kind)
        if not ok:
            reasons[why or "invalid"] += 1
            if kind == "posts" and why == "short_text" and html_fallback:
                fallback_urls.append((it.get("link") or "", it.get("date") or ""))
            continue
        rows.append(row)

    # REST content 为空/过短（主题渲染、PDF 正文等）时，回退抓页面 HTML 做 trafilatura 抽取；
    # 只接受有标题且 trafilatura 正文 >= 阈值的结果，避免把导航/列表页当文章。
    if html_fallback and fallback_urls:
        from crawler.extract import extract_article
        import trafilatura
        from crawler.web import Fetcher
        fetcher = Fetcher(site)
        saved_fb = 0
        for url, date in fallback_urls:
            if not url:
                continue
            for u, st, body in fetcher.stream_many([url], workers=1):
                if st != 200 or not body:
                    continue
                txt = trafilatura.extract(body) or ""
                if len(txt) < 120:
                    continue
                row = extract_article(u, body, site)
                row["text"] = txt
                row = normalize(row, site)
                ok, why = validate(row, site, kind)
                if ok and row.get("title"):
                    rows.append(row)
                    saved_fb += 1
        log(f"  HTML 回退补到 {saved_fb} 条（REST 短正文 {len(fallback_urls)} 条中）")
    written = append_rows(dom, rows, kind) if rows else 0
    log(f"  写入 {written} 条新行；丢弃原因 {dict(reasons)}")
    return {"domain": dom, "kind": kind, "missing": len(missing), "saved": written,
            "reasons": dict(reasons), "dry_run": False}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", action="append", required=True, help="domain（可重复）")
    ap.add_argument("--kind", default="posts", choices=["posts", "pages", "media"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--html-fallback", action="store_true",
                    help="REST 正文过短时回退抓页面 HTML（仅 posts）")
    a = ap.parse_args()
    sites = {s["domain"]: s for s in yaml.safe_load(open(os.path.join(HERE, "sources.yaml"), encoding="utf-8")) or []}
    for d in a.site:
        site = sites.get(d)
        if not site:
            sys.exit(f"site {d!r} not in sources.yaml")
        if site.get("channel") != "wp-api":
            print(f"skip {d}: channel={site.get('channel')}（只支持 wp-api）", flush=True)
            continue
        print(json.dumps(backfill(site, a.kind, a.dry_run, a.html_fallback), ensure_ascii=False))


if __name__ == "__main__":
    main()
