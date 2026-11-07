import html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests, trafilatura
from crawler.config import ROOT, DATA, DB, UA, COLS, MEDIA_COLS, WORKERS
from crawler import store, discover, mapping, extract as extract_mod, web, cursor
from crawler.store import (load_cursor, save_cursor, load_known, append_rows,
                           existing_urls, save, db, _paths)
from crawler.discover import sitemap_urls, rss_urls, html_urls, wp_page_url
from crawler.mapping import map_wp_post, map_wp_media, map_json_item, _flatten_rich
from crawler.extract import extract_article
from crawler.web import Fetcher, _get, wp_json, cloud_get, headers_for
from crawler.pipeline import normalize, validate

# 空页抖动：同页重试 N 次；仍空就跳过该页（记 reasons.empty_page），继续翻。
EMPTY_RETRIES = int(os.environ.get("WP_EMPTY_RETRIES", "3"))
EMPTY_RETRY_SLEEP = float(os.environ.get("WP_EMPTY_RETRY_SLEEP", "1.5"))
# 连续这么多页都是空页 → 站点很可能在吐挑战页/假空页，停止并标 interrupted，
# 不能一路翻到天荒地老，更不能标 complete。
EMPTY_PAGE_LIMIT = int(os.environ.get("WP_EMPTY_PAGE_LIMIT", "5"))


def _int_header(hdr, name):
    try:
        return int((hdr or {}).get(name))
    except (TypeError, ValueError):
        return None


def _parse_page(st, body):
    """把一次分页响应归类 -> (kind, payload)。

    kind: ok(带 posts) | empty(200 但 []) | end(400 invalid page) |
          blocked(401/403) | retry(其他瞬时失败/非 JSON)
    """
    text = body if isinstance(body, str) else ""
    if st == 400 and "rest_post_invalid_page_number" in text:
        return "end", None
    if st in (401, 403):
        return "blocked", "http_%s" % st
    if st != 200:
        return "retry", "http_%s" % st
    posts = web.parse_json_tolerant(text)
    if posts is None:
        return "retry", "non_json"       # 挑战页/坏响应：不是"到底"
    if not isinstance(posts, list):
        return "retry", "bad_json"
    if not posts:
        return "empty", None
    return "ok", posts


def _retry_empty_page(site, page, kind, impersonate):
    """200 [] 可能是分页抖动：同页再试 N 次，返回 posts 或 None。"""
    for _ in range(max(0, EMPTY_RETRIES)):
        if EMPTY_RETRY_SLEEP:
            time.sleep(EMPTY_RETRY_SLEEP)
        st, body, _hdr = _get(None, wp_page_url(site, page, kind=kind), timeout=60,
                              impersonate=impersonate, headers=headers_for(site))
        page_kind, info = _parse_page(st, body)
        if page_kind == "ok":
            return info
    return None


def _fetch_pages(site, pages, kind, impersonate):
    headers = headers_for(site)
    with ThreadPoolExecutor(max_workers=max(1, len(pages))) as ex:
        return list(ex.map(lambda p: _get(None, wp_page_url(site, p, kind=kind),
                                          impersonate=impersonate, headers=headers), pages))


def _collect_rows(posts, site, kind, known):
    rows = []
    for post in posts:
        row = map_wp_media(post, site) if kind == "media" else map_wp_post(post, site)
        if kind == "media":
            if not row["url"] or row["url"] in known:
                continue
        elif (kind == "posts" and len(row["text"]) < 100) or row["url"] in known:
            continue
        known.add(row["url"])
        rows.append(row)
    return rows


def _result(domain, cur, saved, t0, reasons, skipped_pages):
    return {"domain": domain, "saved": saved, "total_saved": cur.get("saved", 0),
            "next_page": cur.get("next_page"), "total_pages": cur.get("total_pages"),
            "done": cur.get("done", False), "status": cur.get("status"),
            "reasons": dict(reasons), "skipped_pages": skipped_pages,
            "secs": round(time.time() - t0, 1)}


def collect_wp(site, limit=0, workers=8, progress=True, kind="posts"):
    """Page-parallel WordPress collection with an atomic cursor; safe to kill and rerun.

    2026-11 事故后的 done 语义：空页/挑战页 **不是**"到底"。
    - 200 []：同页重试 EMPTY_RETRIES 次，仍空则跳过该页继续翻（reasons.empty_page）；
    - 只有 next_page 翻过 total_pages，或拿到 400 rest_post_invalid_page_number，
      且本次没有跳过页，才 mark_complete；
    - 跳过页 / 瞬时失败 → partial / interrupted / blocked，游标留在可续跑的位置。
    """
    impersonate = site.get("via") == "impersonate"
    domain = site["domain"]
    cur = cursor.load(domain, kind)
    if cursor.is_complete(domain, kind):
        return {"domain": domain, "skipped": "done", "saved": 0, "status": cur.get("status")}
    known = load_known(domain, kind)
    reasons = Counter(cur.get("reasons") or {})
    skipped_pages = int(cur.get("skipped_pages") or 0)
    total_pages = cur.get("total_pages") or None
    total_posts = cur.get("total_posts")
    t0, saved = time.time(), 0
    prefetched = {}

    # 首屏探一次 total_pages；顺带识别 403/挑战页/真空站。
    if not total_pages and not total_posts:
        st, body, hdr = _get(requests.Session(), wp_page_url(site, 1, kind=kind), timeout=90,
                             impersonate=impersonate, headers=headers_for(site))
        page_kind, info = _parse_page(st, body)
        total_pages = _int_header(hdr, "x-wp-totalpages") or None
        total_posts = _int_header(hdr, "x-wp-total")
        if page_kind == "blocked":
            cur = cursor.mark_blocked(domain, kind, info, reasons=reasons,
                                      last_error=(body or "")[:120])
            return _result(domain, cur, saved, t0, reasons, skipped_pages)
        if page_kind == "end" or (page_kind == "empty" and total_posts == 0 and not total_pages):
            cur = cursor.mark_complete(domain, kind, total_pages=total_pages,
                                       total_posts=total_posts or 0, reasons=reasons,
                                       next_page=1)
            return _result(domain, cur, saved, t0, reasons, skipped_pages)
        if page_kind == "retry":
            cur = cursor.mark_interrupted(domain, kind, info, reasons=reasons,
                                          last_error=(body or "")[:120])
            return {"domain": domain, "error": "first page %s %s" % (st, (body or "")[:100]),
                    "status": cur.get("status"), "done": cur.get("done", False),
                    "reasons": dict(reasons)}
        prefetched[1] = (st, body, hdr)
        cur = cursor.save(domain, kind, total_pages=total_pages,
                          total_posts=total_posts if total_posts is not None else cur.get("total_posts"),
                          reasons=dict(reasons))

    next_page = int(cur.get("next_page") or 1)
    final = None
    empty_streak = 0
    while True:
        start = int(cur.get("next_page") or 1)
        final = None
        if total_pages and start > total_pages:
            final = "complete" if not skipped_pages else "partial"
            next_page = start
            break
        pages = list(range(start, start + max(1, workers)))
        if total_pages:
            pages = [p for p in pages if p <= total_pages]
        if not pages:
            final = "complete" if not skipped_pages else "partial"
            next_page = start
            break

        results = {p: prefetched[p] for p in pages if p in prefetched}
        missing = [p for p in pages if p not in prefetched]
        if missing:
            for p, res in zip(missing, _fetch_pages(site, missing, kind, impersonate)):
                results[p] = res
        prefetched.clear()

        batch_rows, last_ok, terminal = [], start - 1, None
        for p in pages:
            st, body, hdr = results[p]
            if not total_pages:
                total_pages = _int_header(hdr, "x-wp-totalpages") or total_pages
            if total_posts is None:
                total_posts = _int_header(hdr, "x-wp-total")
            page_kind, info = _parse_page(st, body)

            if page_kind == "end":
                last_ok = p
                terminal = ("complete", None)
                break
            if page_kind == "blocked":
                terminal = ("blocked", info)
                break
            if page_kind == "retry":
                terminal = ("interrupted", info)
                break
            if page_kind == "empty":
                posts = _retry_empty_page(site, p, kind, impersonate)
                if not posts:
                    empty_streak += 1
                    if empty_streak >= EMPTY_PAGE_LIMIT:
                        # 连续空页更像挑战页/站点故障：中断在这页，下次重试
                        last_ok = p - 1
                        terminal = ("interrupted", "empty_pages")
                        break
                    # 持久空页：跳过它继续翻，但本次不能再标 complete
                    skipped_pages += 1
                    reasons["empty_page"] += 1
                    last_ok = p
                    continue
                page_kind, info = "ok", posts

            posts = info
            empty_streak = 0
            cur["seen"] = int(cur.get("seen") or 0) + len(posts)
            batch_rows.extend(_collect_rows(posts, site, kind, known))
            last_ok = p
            if limit and saved + len(batch_rows) >= limit:
                terminal = ("paused", "limit")
                break

        written = 0
        if batch_rows:
            written = append_rows(domain, batch_rows, kind)
            if not isinstance(written, int):
                written = len(batch_rows)
        saved += written
        cur["saved"] = int(cur.get("saved") or 0) + written

        if terminal:
            final, reason = terminal
            if final == "complete" and skipped_pages:
                final = "partial"
            elif final in ("interrupted", "blocked") and reason in ("http_401", "http_403"):
                final = "blocked"
            elif final == "blocked" and (saved or cur.get("saved")):
                final = "partial"          # 抓到一半才被墙：数据保留，状态 partial
        else:
            reason = None
            if total_pages and last_ok + 1 > total_pages:
                final = "complete" if not skipped_pages else "partial"
        next_page = last_ok + 1 if last_ok >= start else start

        fields = dict(next_page=next_page, total_pages=total_pages, total_posts=total_posts,
                      saved=cur["saved"], seen=cur.get("seen"), skipped_pages=skipped_pages,
                      reasons=dict(reasons))
        if final:
            if final == "partial" and not reasons.get("skipped_pages") and skipped_pages:
                fields["last_reason"] = "skipped_pages"
            elif reason:
                fields["last_reason"] = reason
            cur = cursor.save(domain, kind, status=final, **fields)
        else:
            cur = cursor.save(domain, kind, status="interrupted", **fields)
        if progress:
            rate = saved / max(1e-9, time.time() - t0)
            print(f"  {domain} page {last_ok}/{total_pages or '?'} saved={cur['saved']} "
                  f"{rate:.1f}/s", flush=True)
        if final:
            break
    if final is None:
        final = "interrupted"
    if cur.get("status") != final or cur.get("next_page") != next_page:
        cur = cursor.save(domain, kind, status=final, next_page=next_page,
                          total_pages=total_pages, total_posts=total_posts,
                          saved=cur.get("saved", 0), seen=cur.get("seen"),
                          skipped_pages=skipped_pages, reasons=dict(reasons))
    return _result(domain, cur, saved, t0, reasons, skipped_pages)


def run(site, limit, log, fetcher, kind="posts"):
    """wp-api channel entry point; `via: browser` keeps the legacy page loop."""
    if site.get("via") in ("browser", "ego"):
        return _legacy_browser(site, limit, kind, log, fetcher)
    return collect_wp(site, limit=limit, workers=WORKERS, kind=kind)


def _legacy_browser(site, limit, kind, log, fetcher):
    """Cloudflare/Akamai sites: page through the REST API inside a real browser.

    与 collect_wp 同一套 done 语义：空页重试后跳过（最终 partial），翻过 total_pages
    或拿到 400 invalid page 才算 complete；失败原因写 cursor.reasons。
    """
    domain = site["domain"]
    cur = cursor.load(domain, kind)
    if cursor.is_complete(domain, kind):
        return {"domain": domain, "channel": "wp-api", "skipped": "done", "saved": 0,
                "status": cur.get("status")}
    known = load_known(domain, kind)
    reasons = Counter(cur.get("reasons") or {})
    skipped_pages = int(cur.get("skipped_pages") or 0)
    total_pages = cur.get("total_pages") or None
    per = 50
    batch_pages = int(os.environ.get("EGO_PAGES", "4"))
    saved, t0 = 0, time.time()
    next_page = int(cur.get("next_page") or 1)
    empty_streak = 0
    final = None

    def fetch(pages):
        urls = [wp_page_url(site, p, per=per, kind=kind) for p in pages]
        return list(fetcher.get_many(urls, timeout=90))

    while True:
        start = int(cur.get("next_page") or 1)
        final = None
        if total_pages and start > total_pages:
            final = "complete" if not skipped_pages else "partial"
            next_page = start
            break
        pages = list(range(start, start + max(1, batch_pages)))
        if total_pages:
            pages = [p for p in pages if p <= total_pages]
        if not pages:
            final = "complete" if not skipped_pages else "partial"
            next_page = start
            break
        results = dict(zip(pages, fetch(pages)))
        batch_rows, last_ok, terminal = [], start - 1, None
        for p in pages:
            _u, st, body = results.get(p) or (None, 0, "")
            page_kind, info = _parse_page(st, body)
            if page_kind == "end":
                last_ok = p
                terminal = ("complete", None)
                break
            if page_kind == "blocked":
                terminal = ("blocked", info)
                break
            if page_kind == "retry":
                terminal = ("interrupted", info)
                break
            if page_kind == "empty":
                posts = None
                for _ in range(max(0, EMPTY_RETRIES)):
                    if EMPTY_RETRY_SLEEP:
                        time.sleep(EMPTY_RETRY_SLEEP)
                    again = list(fetcher.get_many([wp_page_url(site, p, per=per, kind=kind)], timeout=90))
                    if again:
                        k2, i2 = _parse_page(again[0][1], again[0][2])
                        if k2 == "ok":
                            posts = i2
                            break
                if not posts:
                    empty_streak += 1
                    if empty_streak >= EMPTY_PAGE_LIMIT:
                        last_ok = p - 1
                        terminal = ("interrupted", "empty_pages")
                        break
                    skipped_pages += 1
                    reasons["empty_page"] += 1
                    last_ok = p
                    continue
                page_kind, info = "ok", posts

            posts = info
            empty_streak = 0
            cur["seen"] = int(cur.get("seen") or 0) + len(posts)
            for post in posts:
                row = normalize(map_wp_post(post, site), site)
                ok, why = validate(row, site, kind)
                if not ok:
                    reasons[why or "invalid_row"] += 1
                    continue
                if row["url"] in known:
                    continue
                known.add(row["url"])
                batch_rows.append(row)
            last_ok = p
            if limit and saved + len(batch_rows) >= limit:
                terminal = ("paused", "limit")
                break
        if batch_rows:
            append_rows(domain, batch_rows, kind)
        saved += len(batch_rows)
        cur["saved"] = int(cur.get("saved") or 0) + len(batch_rows)
        if terminal:
            final, reason = terminal
            if final == "complete" and skipped_pages:
                final = "partial"
            elif final in ("interrupted", "blocked") and reason in ("http_401", "http_403"):
                final = "blocked"
            elif final == "blocked" and (saved or cur.get("saved")):
                final = "partial"
        else:
            reason = None
            if total_pages and last_ok + 1 > total_pages:
                final = "complete" if not skipped_pages else "partial"
        next_page = last_ok + 1 if last_ok >= start else start
        fields = dict(next_page=next_page, total_pages=total_pages, saved=cur["saved"],
                      seen=cur.get("seen"), skipped_pages=skipped_pages,
                      reasons=dict(reasons))
        if final:
            if final == "partial" and skipped_pages and not reason:
                fields["last_reason"] = "skipped_pages"
            elif reason:
                fields["last_reason"] = reason
            cur = cursor.save(domain, kind, status=final, **fields)
        else:
            cur = cursor.save(domain, kind, status="interrupted", **fields)
        log.info("page=%s/%s saved=%d status=%s reasons=%s", last_ok, total_pages, cur["saved"],
                 final or "running", dict(reasons))
        if final:
            break
    if final is None:
        final = "interrupted"
    if cur.get("status") != final or cur.get("next_page") != next_page:
        cur = cursor.save(domain, kind, status=final, next_page=next_page,
                          total_pages=total_pages, saved=cur.get("saved", 0),
                          seen=cur.get("seen"), skipped_pages=skipped_pages,
                          reasons=dict(reasons))
    return {"domain": domain, "channel": "wp-api", "discovered": total_pages or 0,
            "saved": saved, "errors": sum(reasons.values()), "total": cur.get("saved", 0),
            "status": final, "reasons": dict(reasons), "secs": round(time.time() - t0, 1)}
