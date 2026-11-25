"""Shared loop for URL-list channels (sitemap / rss / html): queue -> fetch -> extract -> store.

Uses the durable RequestQueue so a failed page is retried on its own and large
sitemaps are streamed in batches instead of being held in memory.

done 语义（2026-11 事故后）：
- 发现阶段 0 条 URL（504/正则不匹配/挑战页）→ interrupted，**绝不标 complete**（wallis）；
- 队列清空 **且本次确有产出** → complete；队列空但没有产出 → partial（no_output）；
- 队列还剩工作（limit 到了 / 有 unprocessed）→ paused / interrupted；
- 每个 URL 的失败原因计数写进 cursor.reasons，不再只留在内存 Counter。
"""
import os, re, time
from collections import Counter

from crawler import cursor, paywall
from crawler.htmlpage import page_error, paywall_signal, structured_body
from crawler.extract import extract_best, extract_metadata
from crawler.quality import DetailCounts
from crawler.pipeline import normalize, validate
from crawler.request import RequestQueue
from crawler.store import append_rows, load_known

BATCH = int(os.environ.get("QUEUE_BATCH", "200"))
FLUSH_EVERY = 50
AUTH_REASONS = ("http_401", "http_403")
RETRY_COOLDOWN = int(os.environ.get("RETRY_COOLDOWN", "900"))   # 跨运行重试临时故障前的冷却
RETRYABLE_HTTP = {0, 408, 425, 429, 500, 502, 503, 504}
GONE_HTTP = {404, 410}
DISCOVERY_FAILED_MAX = 50        # cursor 里最多记 50 个失败入口，避免撑爆游标


def _count(reasons, run_reasons, key):
    reasons[key] += 1
    run_reasons[key] += 1


def _split_discovery_failures(failures):
    """把发现阶段的失败入口分成"可重试"和"已消失（404/410）"两组。"""
    retryable, gone = [], []
    for f in failures or []:
        try:
            status = int(f.get("status") or 0)
        except Exception:
            status = 0
        if status in GONE_HTTP:
            gone.append(f)
        else:
            retryable.append(f)
    return retryable, gone


def _failed_sample(failures):
    return [{"url": f.get("url", ""), "status": f.get("status")} for f in failures[:DISCOVERY_FAILED_MAX]]


def _dead_letters(domain, kind):
    """队列里终态 error 行的错误信息（用于区分 404/410 死链与可重试故障）。"""
    from crawler.request import RequestQueue
    q = RequestQueue(domain, kind)
    try:
        rows = q.conn.execute(
            "SELECT error FROM requests WHERE domain=? AND kind=? AND state='error'",
            (domain, kind)).fetchall()
    except Exception:
        return []
    return [str(r[0] or "") for r in rows]


def _gone_404(errors):
    """死信是否都是 404/410（链接已删除，不是采集失败）。"""
    return bool(errors) and all(re.match(r"^http_(404|410)\b", e) for e in errors)


def collect_urls(site, urls, limit, log, fetcher, kind="posts", discovered_count=None,
                 discovery_failures=None):
    domain = site["domain"]
    channel = site.get("channel", "")
    counts = DetailCounts(discovered=len(urls) if discovered_count is None else discovered_count)
    paywall.reset_budget()
    retryable_disc, gone_disc = _split_discovery_failures(discovery_failures)
    disc_reasons = Counter("discovery_http_%s" % f.get("status") for f in (discovery_failures or []))
    cur = cursor.load(domain, kind)
    known_urls = load_known(domain, kind)
    saved_count_changed = cur["saved"] != len(known_urls)
    cur["saved"] = len(known_urls)
    q = RequestQueue(domain, kind)
    repaired = q.requeue_missing_rows(known_urls)
    if repaired:
        log.info("repair_missing_rows domain=%s count=%d", domain, repaired)
    q.seed_done(known_urls, channel=channel)
    if site.get("documents"):
        q.retry_documents()
    requeued = q.retry_transient(cooldown=RETRY_COOLDOWN, statuses=site.get("retry_http_statuses", ()))
    if requeued:
        log.info("retry_transient domain=%s requeued=%d", domain, requeued)
    if cur["status"] == "complete" and not urls:
        current = q.stats()
        # 已完成站点若本次发现阶段有可重试失败（子 sitemap 503 等），不能继续显示 complete
        if retryable_disc:
            reasons = Counter(cur.get("reasons") or {})
            reasons.update(disc_reasons)
            reasons["discovery_failed"] += 1
            cur = cursor.save(domain, kind, status="partial", reasons=dict(reasons),
                              discovery_failed=_failed_sample(retryable_disc),
                              last_reason="discovery_failed")
            log.info("reopen domain=%s reason=discovery_failed failed=%d",
                     domain, len(retryable_disc))
            return {"domain": domain, "channel": channel, "quality": counts.report(), "skipped": "reopened", "saved": 0,
                    "status": "partial", "reasons": dict(reasons),
                    "discovery_failed": _failed_sample(retryable_disc)}
        if (not current.get("unprocessed") and not current.get("skipped") and discovered_count
                and (not current.get("error") or _gone_404(_dead_letters(domain, kind)))):
            if saved_count_changed or cur.get("queue") != current:
                cursor.save(domain, kind, saved=cur["saved"], queue=current)
            return {"domain": domain, "channel": channel, "quality": counts.report(), "skipped": "done", "saved": 0,
                    "status": "complete", "reasons": dict(cur.get("reasons") or {})}

    reasons = Counter(cur.get("reasons") or {})
    reasons.update(disc_reasons)
    run_reasons = Counter()
    added = q.add_many(urls, channel=channel) if urls else 0
    current = q.stats()
    pending_start = current.get("unprocessed", 0)
    discovered = sum(current.values())
    if discovered_count is None:
        discovered_count = len(urls)

    if pending_start == 0:
        # 队列里没有待办。两种情况必须分开：
        #   发现阶段拿到 0 条 URL（504/挑战页/正则失效）→ interrupted，绝不能当"已采完"（wallis 事故）
        #   发现成功、但所有 URL 都已在盘上（nothing_new）→ complete（老游标缺 done 的站靠这条收尾）
        dead_errs = _dead_letters(domain, kind)
        if discovered_count == 0:
            status, reason = "interrupted", "discovery_empty"
        elif dead_errs and not _gone_404(dead_errs):
            status, reason = "partial", "dead_letters"     # 429/5xx 等，仍需重试
        elif current.get("skipped"):
            status, reason = "partial", "skipped"
        else:
            status, reason = "complete", ("dead_404" if dead_errs else "nothing_new")
        reasons[reason] += 1
        if cur["status"] in ("blocked", "paused"):
            status = cur["status"]             # 保留阻断/暂停信息，别降级成"重来"
        elif cur["status"] == "partial" and not (dead_errs and _gone_404(dead_errs)):
            # 之前的 partial 是"有缺量"（空页/短正文等），不能因为本次没新工作就洗成 complete；
            # 唯一例外：剩下的终态错误全是 404/410（页面已删除）。
            status, reason = "partial", (cur.get("last_reason") or "kept_partial")
        if retryable_disc and status == "complete":
            # 发现阶段有可重试失败（子 sitemap 503/超时）→ 任务保持不完整，等下次补
            status, reason = "partial", "discovery_failed"
            reasons["discovery_failed"] += 1
        cur = cursor.save(domain, kind, status=status, reasons=dict(reasons),
                          discovered=discovered_count, next_page=1, total_pages=1,
                          last_reason=reason, saved=cur.get("saved", 0),
                          queue=current, dead_letters=current.get("error", 0),
                          skipped=current.get("skipped", 0),
                          discovery_failed=_failed_sample(retryable_disc))
        log.info("no work domain=%s reason=%s status=%s queue=%s", domain, reason, status,
                 q.stats())
        return {"domain": domain, "channel": channel, "quality": counts.report(), "discovered": 0, "saved": 0,
                "errors": 0, "total": cur.get("saved", 0), "queue": q.stats(),
                "reasons": dict(reasons), "status": status,
                "discovery_failed": _failed_sample(retryable_disc), "secs": 0.0}

    workers = int(os.environ.get("SITE_WORKERS", "4"))
    saved = errors = 0
    buf = []                                   # [(queue key, row)]，写盘成功后才标 done
    t0 = time.time()
    last_activity = time.monotonic()

    def flush():
        """先把整批写进 articles/urls，成功后才把队列行标 done（审查问题 1）。"""
        nonlocal saved
        if not buf:
            return
        rows = [row for _, row in buf]
        written = append_rows(domain, rows, kind)      # 失败会抛异常：buf 不清、不标 done
        q.mark_done([key for key, _ in buf])
        saved += written if isinstance(written, int) else len(rows)
        buf.clear()
        log.info("saved=%d elapsed=%.0fs queue=%s", saved, time.time() - t0, q.stats())

    while True:
        want = (limit - saved - len(buf)) if limit else BATCH
        if want <= 0:
            break
        batch = q.next_batch(min(BATCH, max(1, want)))
        if not batch:
            break
        counts.attempted += len(batch)
        responded = set()
        for req, status, body in fetcher.stream_requests(batch, workers=workers):
            key = req.key
            responded.add(key)
            now = time.monotonic()
            if now - last_activity >= 30:
                flush()
                log.info("request progress domain=%s processed=%d/%d", domain, len(responded), len(batch))
                last_activity = now
            rejection = page_error(body) if status == 200 and isinstance(body, str) else ""
            if rejection:
                status = 403 if rejection == "challenge" else 503
            if (status != 200 and isinstance(body, str) and paywall.enabled(site)
                    and not q.permanent_error(f"http_{status} {body}")):
                # 403 / 挑战页：按站点开关试 13ft 链路（Googlebot → 存档 / Freedium）
                new_status, new_body, source = paywall.recover(req.url, site, body, status)
                if source:
                    status, body, rejection = new_status, new_body, ""
                    _count(reasons, run_reasons, "paywall_" + source)
                    log.info("paywall recovered domain=%s url=%s source=%s", domain, req.url, source)
            if status == 200 and isinstance(body, str):
                # 付费墙检测只对配了 premium_marker 的站生效（保持其它站点的既有行为）
                marker = site.get("premium_marker", "")
                restriction = paywall_signal(body, marker) if marker else ""
                if restriction and paywall.enabled(site):
                    hit = restriction
                    _, new_body, source = paywall.recover(req.url, site, body, status)
                    if source:
                        body, restriction, rejection = new_body, "", ""
                        _count(reasons, run_reasons, "paywall_" + source)
                        log.info("paywall recovered domain=%s url=%s source=%s restriction=%s",
                                 domain, req.url, source, hit)
                if restriction and structured_body(body):
                    restriction = ""          # 前端墙：__NEXT_DATA__/JSON-LD 里有全文，照常提取
                if restriction:
                    q.mark_skipped([key], "access_restricted")
                    counts.rejections["access_restricted"] += 1
                    _count(reasons, run_reasons, restriction)
                    continue
            if status != 200:
                errors += 1
                _count(reasons, run_reasons, "http_%s" % status)
                counts.request_failures[f"http_{status}"] += 1
                q.mark_error([key], {key: f"http_{status} {(body if isinstance(body, str) else '')[:200]}"})
                continue
            if isinstance(body, bytes):
                from crawler.documents import extract_pdf
                try:
                    row = extract_pdf(req.url, body, site)
                except Exception as exc:
                    q.mark_error([key], {key: f"document_error {exc}"})
                    errors += 1
                    _count(reasons, run_reasons, "document_error")
                    counts.rejections["document_error"] += 1
                    continue
            else:
                row = extract_best(req.url, body, site, min_text=0)
                if row is None:
                    row = extract_metadata(req.url, body, site)   # 元数据兜底：无正文也保留条目
            if row is None:
                q.mark_skipped([key], "no_text")
                _count(reasons, run_reasons, "no_text")
                counts.rejections["no_text"] += 1
                continue
            row = normalize(row, site)
            ok, why = validate(row, site, kind)
            if not ok:
                q.mark_skipped([key], why)
                _count(reasons, run_reasons, why or "invalid_row")
                counts.rejections[why or "invalid_row"] += 1
                continue
            buf.append((key, row))
            if len(buf) >= FLUSH_EVERY:
                flush()
        missing = [req.key for req in batch if req.key not in responded]
        if missing:
            q.mark_error(missing, {key: "http_0 no_response" for key in missing})
            errors += len(missing)
            counts.request_failures["no_response"] += len(missing)
            reasons["no_response"] += len(missing)
            run_reasons["no_response"] += len(missing)
        if limit and saved + len(buf) >= limit:
            break
        flush()

    flush()

    st = q.stats()
    unprocessed = st.get("unprocessed", 0)
    dead = st.get("error", 0)
    total_saved = int(cur.get("saved") or 0) + saved
    last_reason = ""

    if unprocessed:
        status = "paused" if (limit and saved >= limit) else "interrupted"
        last_reason = "limit" if status == "paused" else "pending"
    elif saved:
        dead_errs = _dead_letters(domain, kind)
        if dead and not _gone_404(dead_errs):
            status, last_reason = "partial", "dead_letters"
        else:
            status, last_reason = "complete", ("dead_404" if dead else "")
    elif pending_start or added:
        # 队列清空但零产出（全是 no_text/403/无效行）：不是"采完了"
        status = "partial"
        last_reason = "no_output"
    else:
        status = "interrupted"
        last_reason = "no_output"
    if st.get("skipped") and status == "complete":
        status, last_reason = "partial", "skipped"
    auth = sum(run_reasons.get(k, 0) for k in AUTH_REASONS)
    all_http = sum(v for k, v in run_reasons.items() if k.startswith("http_"))
    if saved == 0 and unprocessed == 0 and auth and auth == all_http:
        status = "blocked"                     # 403/登录墙：整批都拿不到
        last_reason = "http_401" if run_reasons.get("http_401") else "http_403"
    if retryable_disc and status == "complete":
        # 子 sitemap 503/超时等：其它子图有产出也不能算完整（审查问题 2）
        status, last_reason = "partial", "discovery_failed"
        reasons["discovery_failed"] += 1

    fields = dict(status=status, reasons=dict(reasons), discovered=max(discovered, discovered_count or 0),
                  saved=total_saved, next_page=1, total_pages=1, queue=st,
                  dead_letters=dead, skipped=st.get("skipped", 0),
                  discovery_failed=_failed_sample(retryable_disc))
    if last_reason:
        fields["last_reason"] = last_reason
    cur = cursor.save(domain, kind, **fields)
    log.info("done status=%s saved=%d errors=%d reasons=%s queue=%s", status, saved, errors,
             dict(reasons), st)
    return {"domain": domain, "channel": channel, "quality": counts.report(), "discovered": discovered,
            "saved": saved, "errors": errors, "total": total_saved, "queue": st,
            "reasons": dict(reasons), "status": status,
            "discovery_failed": _failed_sample(retryable_disc),
            "secs": round(time.time() - t0, 1)}
