"""Shared loop for URL-list channels (sitemap / rss / html): queue -> fetch -> extract -> store.

Uses the durable RequestQueue so a failed page is retried on its own and large
sitemaps are streamed in batches instead of being held in memory.

done 语义（2026-11 事故后）：
- 发现阶段 0 条 URL（504/正则不匹配/挑战页）→ interrupted，**绝不标 complete**（wallis）；
- 队列清空 **且本次确有产出** → complete；队列空但没有产出 → partial（no_output）；
- 队列还剩工作（limit 到了 / 有 unprocessed）→ paused / interrupted；
- 每个 URL 的失败原因计数写进 cursor.reasons，不再只留在内存 Counter。
"""
import os, time
from collections import Counter

from crawler import cursor
from crawler.extract import extract_best
from crawler.pipeline import normalize, validate
from crawler.request import RequestQueue
from crawler.store import append_rows, load_known

BATCH = int(os.environ.get("QUEUE_BATCH", "200"))
FLUSH_EVERY = 50
AUTH_REASONS = ("http_401", "http_403")


def _count(reasons, run_reasons, key):
    reasons[key] += 1
    run_reasons[key] += 1


def collect_urls(site, urls, limit, log, fetcher, kind="posts"):
    domain = site["domain"]
    channel = site.get("channel", "")
    cur = cursor.load(domain, kind)
    if cur["status"] == "complete":
        return {"domain": domain, "channel": channel, "skipped": "done", "saved": 0,
                "status": "complete", "reasons": dict(cur.get("reasons") or {})}

    reasons = Counter(cur.get("reasons") or {})
    run_reasons = Counter()
    q = RequestQueue(domain, kind)
    q.seed_done(load_known(domain, kind), channel=channel)
    added = q.add_many(urls, channel=channel) if urls else 0
    pending_start = q.stats().get("unprocessed", 0)
    discovered = added + pending_start

    if pending_start == 0:
        # 没有队列工作：发现 0 条 URL 或全部已知。发现失败绝不能当"已采完"。
        already = q.stats().get("done", 0)
        reason = "discovery_empty" if (urls or not already) else "nothing_new"
        reasons[reason] += 1
        status = "interrupted"
        if cur["status"] in ("partial", "blocked", "paused"):
            status = cur["status"]             # 保留已知缺量/阻断信息，别降级成"重来"
        cur = cursor.save(domain, kind, status=status, reasons=dict(reasons),
                          discovered=0, next_page=1, total_pages=1,
                          last_reason=reason, saved=cur.get("saved", 0))
        log.info("no work domain=%s reason=%s status=%s queue=%s", domain, reason, status,
                 q.stats())
        return {"domain": domain, "channel": channel, "discovered": 0, "saved": 0,
                "errors": 0, "total": cur.get("saved", 0), "queue": q.stats(),
                "reasons": dict(reasons), "status": status, "secs": 0.0}

    workers = int(os.environ.get("SITE_WORKERS", "4"))
    saved = errors = 0
    buf = []
    t0 = time.time()

    while True:
        want = (limit - saved - len(buf)) if limit else BATCH
        if want <= 0:
            break
        batch = q.next_batch(min(BATCH, max(1, want)))
        if not batch:
            break
        for req, status, body in fetcher.stream_requests(batch, workers=workers):
            key = req.key
            if status != 200:
                errors += 1
                _count(reasons, run_reasons, "http_%s" % status)
                q.mark_error([key], {key: body if isinstance(body, str) else ""})
                continue
            row = extract_best(req.url, body, site, min_text=0)
            if row is None:
                q.mark_skipped([key], "no_text")
                _count(reasons, run_reasons, "no_text")
                continue
            row = normalize(row, site)
            ok, why = validate(row, site, kind)
            if not ok:
                q.mark_skipped([key], why)
                _count(reasons, run_reasons, why or "invalid_row")
                continue
            buf.append(row)
            q.mark_done([key])
            if len(buf) >= FLUSH_EVERY:
                written = append_rows(domain, buf, kind)
                saved += written if isinstance(written, int) else len(buf)
                buf.clear()
                log.info("saved=%d elapsed=%.0fs queue=%s", saved, time.time() - t0, q.stats())
        if limit and saved + len(buf) >= limit:
            break

    if buf:
        written = append_rows(domain, buf, kind)
        saved += written if isinstance(written, int) else len(buf)

    st = q.stats()
    unprocessed = st.get("unprocessed", 0)
    dead = st.get("error", 0)
    total_saved = int(cur.get("saved") or 0) + saved
    last_reason = ""

    if unprocessed:
        status = "paused" if (limit and saved >= limit) else "interrupted"
        last_reason = "limit" if status == "paused" else "pending"
    elif saved:
        status = "partial" if dead else "complete"
        last_reason = "dead_letters" if dead else ""
    elif pending_start or added:
        # 队列清空但零产出（全是 no_text/403/无效行）：不是"采完了"
        status = "partial"
        last_reason = "no_output"
    else:
        status = "interrupted"
        last_reason = "no_output"
    auth = sum(run_reasons.get(k, 0) for k in AUTH_REASONS)
    all_http = sum(v for k, v in run_reasons.items() if k.startswith("http_"))
    if saved == 0 and unprocessed == 0 and auth and auth == all_http:
        status = "blocked"                     # 403/登录墙：整批都拿不到
        last_reason = "http_401" if run_reasons.get("http_401") else "http_403"

    fields = dict(status=status, reasons=dict(reasons), discovered=discovered,
                  saved=total_saved, next_page=1, total_pages=1, queue=st,
                  dead_letters=dead, skipped=st.get("skipped", 0))
    if last_reason:
        fields["last_reason"] = last_reason
    cur = cursor.save(domain, kind, **fields)
    log.info("done status=%s saved=%d errors=%d reasons=%s queue=%s", status, saved, errors,
             dict(reasons), st)
    return {"domain": domain, "channel": channel, "discovered": discovered,
            "saved": saved, "errors": errors, "total": total_saved, "queue": st,
            "reasons": dict(reasons), "status": status,
            "secs": round(time.time() - t0, 1)}
