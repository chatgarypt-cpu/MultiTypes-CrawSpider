"""List JSON -> detail page -> rows（例如 samoaobserver）。

2026-11-09 性能修复（原实现每页 3 个分类串行、每次裸 requests 重建 TLS，翻到 2020 年
历史页时约 3.5s/页；429 会被静默跳过、进度不可见）：

- 列表请求复用 `crawler.web.SESSION` 连接池 + 每域限速器（403/429 自动降速）；
- 同一页的多个分类并发请求；429/5xx 做有限重试，仍失败则**不推进游标**（下一轮补齐）；
- 进度日志改用调用方传入的 logger（page/saved/errors 可见）；
- 新增配置：`scan_mode: full|incremental`（增量只看前几页新内容）、`known_pages_stop`
  （增量模式下连续 N 页全是已知文章即停）、`max_pages_per_run`（单轮翻页上限，避免长期占槽）。
"""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from crawler.config import DATA, UA
from crawler.extract import extract_best
from crawler.request import canonical_key
from crawler.store import append_rows, load_known
from crawler.web import Fetcher, LIMITER, SESSION

RETRYABLE = (429, 500, 502, 503, 504)


def _get_json(url, retries=2):
    """带连接复用/限速/重试的列表页请求。返回 (data, error)。"""
    for attempt in range(retries + 1):
        LIMITER.acquire(url)
        try:
            r = SESSION.get(url, headers={"User-Agent": UA}, timeout=45)
        except Exception as exc:
            if attempt < retries:
                time.sleep(1.0 + attempt)
                continue
            return None, f"conn:{str(exc)[:60]}"
        if r.status_code == 200:
            LIMITER.reward(url)
            try:
                return r.json(), None
            except Exception:
                return None, "bad_json"
        if r.status_code in (403, 429):
            LIMITER.penalize(url)
        if r.status_code in RETRYABLE and attempt < retries:
            time.sleep(1.5 * (attempt + 1))
            continue
        return None, f"http_{r.status_code}"
    return None, "retries"


def _cat_url(base, pattern, page, cat):
    u = pattern.format(page=page, category=cat) if cat else pattern.format(page=page)
    return u if u.startswith("http") else base + u


def _detail_url(site, base, url_tpl, item, cat):
    cat_name = item.get("category_name") or cat or "news"
    return url_tpl.format(base=base, id=item.get("id"), category_name=cat_name,
                          slug=item.get("slug") or "")


def fetch_page(pat, base, page, cats, items_key="stories", workers=None):
    """并发抓取同一页的多个分类，返回 ({cat: [items]}, [error])。"""
    result, errors = {}, []

    def one(cat):
        data, err = _get_json(_cat_url(base, pat, page, cat))
        return cat, data, err

    with ThreadPoolExecutor(max_workers=workers or max(1, len(cats))) as ex:
        for cat, data, err in ex.map(one, cats):
            if err:
                errors.append({"category": cat, "page": page, "error": err})
                continue
            result[cat] = (data or {}).get(items_key) or (data or {}).get("data") or []
    return result, errors


def collect_json_list(site, limit=0, log=None):
    base = site["base"].rstrip("/")
    cats = site.get("api_categories") or [None]
    pat = site.get("api_list") or ""
    items_key = site.get("api_items") or "stories"
    url_tpl = site.get("api_url") or "{base}/item/{id}"
    free_key = site.get("free_field")
    known = {canonical_key(u) for u in load_known(site["domain"], "posts")}
    fetcher = Fetcher(site)
    log = log or logging.getLogger(site["domain"])

    mode = (site.get("scan_mode") or "full").lower()
    incremental = mode == "incremental"
    known_stop = int(site.get("known_pages_stop", 3) or 0)
    max_pages = int(site.get("max_pages_per_run", 0) or 0)
    total_pages = int(site.get("pages", 100000))

    cur_path = os.path.join(DATA, site["domain"], "jsonlist.cursor.json")
    cur = {}
    try:
        cur = json.load(open(cur_path)) or {}
    except Exception:
        cur = {}
    # 增量模式每次从第 1 页找新内容；全量模式从上次游标继续补历史
    page = 1 if incremental else int(cur.get("page", 1))
    saved = 0
    processed = 0
    known_streak = 0
    t0 = time.time()
    last_error = None

    while page <= total_pages:
        page_items, fetch_errors = fetch_page(pat, base, page, cats, items_key)
        if fetch_errors:
            # 列表页有可重试失败：不推进游标，交给下一轮补齐，并保留失败原因
            last_error = fetch_errors[0]["error"]
            log.warning("%s list page %d errors=%s", site["domain"], page,
                        [e["error"] for e in fetch_errors])
            break
        page_has_items = any(page_items.values())
        urls = []
        for cat in cats:
            for it in page_items.get(cat, []):
                if free_key and it.get(free_key):
                    continue
                link = _detail_url(site, base, url_tpl, it, cat)
                if canonical_key(link) in known:
                    continue
                urls.append((link, it))
        if not page_has_items:
            break                                  # 翻到底了
        if not urls:
            known_streak += 1
            if incremental and known_stop and known_streak >= known_stop:
                log.info("%s incremental stop: %d pages all known", site["domain"], known_streak)
                try:
                    os.makedirs(os.path.dirname(cur_path), exist_ok=True)
                    json.dump({"page": 1}, open(cur_path, "w"))   # 下次仍从第一页找新内容
                except Exception:
                    pass
                page = 1
                break
        else:
            known_streak = 0
            meta = {u: it for u, it in urls}
            rows = []
            for u, status, body in fetcher.stream_many([x[0] for x in urls], workers=4):
                if status != 200 or not body:
                    continue
                if site.get("premium_marker") and site["premium_marker"] in body:
                    continue
                row = extract_best(u, body, site, min_text=120)
                if not row:
                    continue
                it = meta.get(u, {})
                row["title"] = row["title"] or (it.get("heading") or "")
                row["created_at"] = row["created_at"] or (it.get("published_at_iso") or "")
                row["description"] = row["description"] or (it.get("summary") or "")[:500]
                key = canonical_key(row["url"])
                if key in known:
                    continue
                known.add(key)
                rows.append(row)
            if rows:
                written = append_rows(site["domain"], rows, "posts")
                saved += written if isinstance(written, int) else len(rows)
        processed += 1
        log.info("%s json-list page=%d saved=%d pages=%d cats=%s",
                 site["domain"], page, saved, processed, ",".join(str(c) for c in cats))
        try:
            os.makedirs(os.path.dirname(cur_path), exist_ok=True)
            json.dump({"page": (1 if incremental else page + 1)}, open(cur_path, "w"))
        except Exception:
            pass
        if limit and saved >= limit:
            break
        if max_pages and processed >= max_pages:
            break
        page += 1

    result = {"domain": site["domain"], "channel": "json-list", "saved": saved,
              "pages": processed, "total": saved, "secs": round(time.time() - t0, 1)}
    if last_error:
        result["error"] = last_error
    return result


def run(site, limit, log, fetcher, kind="posts"):
    return collect_json_list(site, limit=limit, log=log)
