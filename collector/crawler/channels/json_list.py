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
from crawler import cursor
from crawler.extract import extract_best, extract_metadata
from crawler.htmlpage import boolean_flag, page_error, paywall_signal
from crawler.pipeline import normalize, validate
from crawler.request import canonical_key, RequestQueue
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
            if not isinstance(data, dict) or not isinstance(data.get(items_key, data.get("data")), list):
                errors.append({"category": cat, "page": page, "error": "bad_json_schema"})
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
    include_premium = site.get("include_premium", False)
    known = {canonical_key(u) for u in load_known(site["domain"], "posts")}
    fetcher = Fetcher(site)
    log = log or logging.getLogger(site["domain"])

    mode = (site.get("scan_mode") or "full").lower()
    incremental = mode == "incremental"
    known_stop = int(site.get("known_pages_stop", 3) or 0)
    max_pages = int(site.get("max_pages_per_run", 0) or 0)
    total_pages = int(site.get("pages", 100000))

    q = RequestQueue(site["domain"])
    q.requeue_missing_rows(known)
    q.seed_done(known, channel="json-list")
    # A manual resume should retry transient detail errors from the previous run.
    q.retry_transient(cooldown=int(os.environ.get("RETRY_COOLDOWN", "0")),
                      statuses=site.get("retry_http_statuses", ()))
    cur_path = os.path.join(DATA, site["domain"], "jsonlist.cursor.json")
    cur = {}
    try:
        with open(cur_path, encoding="utf-8") as f:
            cur = json.load(f)
        if (not isinstance(cur, dict) or type(cur.get("page")) is not int or cur["page"] < 1
                or type(cur.get("backfill_revision", 0)) is not int):
            raise ValueError("invalid list cursor")
    except FileNotFoundError:
        cur = {}
    except (OSError, ValueError) as exc:
        reason = f"list_cursor_unreadable: {exc}"
        cursor.mark_interrupted(site["domain"], reason=reason)
        return {"domain": site["domain"], "status": "interrupted", "error": reason, "saved": 0}
    revision = (site.get("backfill_revision") or {}).get("posts", 0)
    if revision > cur.get("backfill_revision", 0):
        cur = {"page": 1, "backfill_revision": revision}
        os.makedirs(os.path.dirname(cur_path), exist_ok=True)
        with open(cur_path + ".tmp", "w") as f:
            json.dump(cur, f)
        os.replace(cur_path + ".tmp", cur_path)
    # 增量模式每次从第 1 页找新内容；全量模式从上次游标继续补历史
    page = 1 if incremental else int(cur.get("page", 1))
    saved = 0
    processed = 0
    known_streak = 0
    t0 = time.time()
    last_error = None
    state = "paused"

    def drain_details():
        nonlocal saved
        batch_size = int(os.environ.get("QUEUE_BATCH", "200"))
        while q.pending() and (not limit or saved < limit):
            requests_ = q.next_batch(min(batch_size, limit - saved) if limit else batch_size)
            by_url = {r.url: r for r in requests_}
            responded, rows, keys = set(), [], []
            batch_keys, labels = set(), []
            for u, status, body in fetcher.stream_many(list(by_url), workers=int(os.environ.get("SITE_WORKERS", "4"))):
                req = by_url[u]
                responded.add(u)
                rejection = page_error(body) if status == 200 and body else ""
                if status != 200 or not body or rejection:
                    if rejection:
                        status = 403 if rejection == "challenge" else 503
                    elif status == 200:
                        status = 0
                    q.mark_error([req.key], {req.key: f"http_{status} {str(body)[:120]}"})
                    continue
                it = json.loads(req.label) if req.label.startswith("{") else {}
                restriction = paywall_signal(body, site.get("premium_marker", ""))
                if restriction:
                    it["detail_access"] = restriction
                    q.update_labels([(req.key, json.dumps(it, ensure_ascii=False))])
                    q.mark_skipped([req.key], "access_restricted")
                    continue
                try:
                    row = extract_best(u, body, site, min_text=0) or extract_metadata(u, body, site)
                    if not row:
                        raise ValueError("no extractable article metadata")
                    row["title"] = row.get("title") or it.get("heading", "")
                    row["created_at"] = row.get("created_at") or it.get("published_at_iso", "")
                    row["description"] = row.get("description") or (it.get("summary") or "")[:500]
                    row = normalize(row, site)
                    valid, reason = validate(row, site, "posts")
                    if not valid:
                        raise ValueError(reason)
                except Exception as exc:
                    q.mark_error([req.key], {req.key: f"extraction_failed {exc}"})
                    continue
                key = canonical_key(row["url"])
                if key not in known and key not in batch_keys:
                    rows.append(row)
                    batch_keys.add(key)
                keys.append(req.key)
                it["detail_access"] = "body_readable" if row.get("text") else "metadata_only"
                labels.append((req.key, json.dumps(it, ensure_ascii=False)))
            for u in set(by_url) - responded:
                req = by_url[u]
                q.mark_error([req.key], {req.key: "http_0 no_response"})
            if rows:
                written = append_rows(site["domain"], rows, "posts")
                saved += written if isinstance(written, int) else len(rows)
                known.update(batch_keys)
            q.update_labels(labels)
            q.mark_done(keys)  # only after persistence succeeds
        return bool(q.conn.execute(
            "SELECT 1 FROM requests WHERE domain=? AND kind='posts' AND "
            "state='error' AND error NOT LIKE 'http_404 %' AND error NOT LIKE 'http_410 %' AND error NOT LIKE 'extraction_failed %' LIMIT 1",
            (site["domain"],)).fetchone())

    # Retry previously discovered details even when the list cursor is already at EOF.
    drain_details()
    while page <= total_pages and (not limit or saved < limit):
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
                premium = boolean_flag(it.get(free_key)) if free_key else None
                if premium is True and not include_premium:
                    continue
                link = _detail_url(site, base, url_tpl, it, cat)
                if canonical_key(link) in known:
                    continue
                label = {k: it.get(k) for k in ("heading", "published_at_iso", "summary")}
                if free_key:
                    label["listing_premium"] = premium
                urls.append((link, json.dumps(label, ensure_ascii=False)))
        if not page_has_items:
            state = "complete"
            break                                  # 翻到底了
        if not urls:
            known_streak += 1
            if incremental and known_stop and known_streak >= known_stop:
                log.info("%s incremental stop: %d pages all known", site["domain"], known_streak)
                os.makedirs(os.path.dirname(cur_path), exist_ok=True)
                with open(cur_path + ".tmp", "w") as f:
                    json.dump({"page": 1, "backfill_revision": revision}, f)
                os.replace(cur_path + ".tmp", cur_path)
                page = 1
                break
        else:
            known_streak = 0
            q.add_labelled(urls, channel="json-list")
            failed = drain_details()
            if failed and not site.get("skip_detail_failures"):
                last_error = "detail_failed"
                break
        processed += 1
        log.info("%s json-list page=%d saved=%d pages=%d cats=%s",
                 site["domain"], page, saved, processed, ",".join(str(c) for c in cats))
        os.makedirs(os.path.dirname(cur_path), exist_ok=True)
        with open(cur_path + ".tmp", "w") as f:
            json.dump({"page": (1 if incremental else page + 1), "backfill_revision": revision}, f)
        os.replace(cur_path + ".tmp", cur_path)
        if limit and saved >= limit:
            break
        if max_pages and processed >= max_pages:
            break
        page += 1

    result = {"domain": site["domain"], "channel": "json-list", "saved": saved,
              "pages": processed, "total": saved, "secs": round(time.time() - t0, 1)}
    if last_error:
        result["error"] = last_error
        state = "interrupted"
    unresolved = q.conn.execute(
        "SELECT COUNT(*) FROM requests WHERE domain=? AND kind='posts' AND "
        "(state IN ('unprocessed','inflight') OR (state='error' AND error NOT LIKE 'http_404 %' AND error NOT LIKE 'http_410 %'))",
        (site["domain"],)).fetchone()[0]
    if state == "complete" and unresolved:
        state, last_error = "partial", "detail_failures"
    restricted = q.conn.execute(
        "SELECT COUNT(*) FROM requests WHERE domain=? AND kind='posts' AND "
        "state='skipped' AND error IN ('access_restricted','premium')", (site["domain"],)).fetchone()[0]
    if state == "complete" and restricted:
        state, last_error = "partial", "access_restricted"
    result["queue"] = q.stats()
    result["access_restricted"] = restricted
    result["status"] = state
    cursor.save(site["domain"], "posts", status=state, saved=len(known),
                last_reason=last_error or ("end_of_list" if state == "complete" else "run_limit"),
                queue=q.stats(), dead_letters=unresolved, access_restricted=restricted,
                backfill_revision=revision)
    return result


def run(site, limit, log, fetcher, kind="posts"):
    return collect_json_list(site, limit=limit, log=log)
