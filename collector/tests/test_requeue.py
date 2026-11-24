"""补采入队：只翻出指定的失败类型，404/410 死链与 PDF 超限不动。"""
import pytest

from crawler.request import RequestQueue


def seed(domain="requeue.test"):
    q = RequestQueue(domain)
    urls = {
        "https://requeue.test/ok": ("done", ""),
        "https://requeue.test/restricted": ("skipped", "access_restricted"),
        "https://requeue.test/forbidden": ("error", "http_403 <html>Access denied</html>"),
        "https://requeue.test/empty": ("error", "http_0 Failed to perform, curl: (52)"),
        "https://requeue.test/gone": ("error", "http_404 gone"),
        "https://requeue.test/bigpdf": ("error", "http_0 PDF exceeds response size limit"),
    }
    q.add_many(list(urls))
    for url, (state, err) in urls.items():
        key = [r.key for r in q.next_batch(50) if r.url == url]
        if not key:
            key = [RequestQueue(domain).conn.execute(
                "SELECT key FROM requests WHERE url=?", (url,)).fetchone()[0]]
        if state == "error":
            q.conn.execute("UPDATE requests SET state='error', error=? WHERE key=?", (err, key[0]))
        elif state == "skipped":
            q.conn.execute("UPDATE requests SET state='skipped', error=? WHERE key=?", (err, key[0]))
        else:
            q.conn.execute("UPDATE requests SET state='done' WHERE key=?", (key[0],))
    q.conn.commit()
    return q


def test_requeue_matches_error_prefixes_only(tmp_env):
    q = seed()
    res = q.requeue_failed(["access_restricted", "http_403"])
    assert res["matched"] == 2
    assert res["requeued"] == 2
    assert res["by_error"] == {"access_restricted": 1, "http_403": 1}
    stats = q.stats()
    assert stats["unprocessed"] == 2
    # 404 / PDF 超限 / 已完成的都没被翻出来
    assert stats["error"] == 3 and stats["done"] == 1
    reopened = {u for (u,) in q.conn.execute(
        "SELECT url FROM requests WHERE state='unprocessed'")}
    assert reopened == {"https://requeue.test/restricted", "https://requeue.test/forbidden"}


def test_requeue_dry_run_writes_nothing(tmp_env):
    q = seed("dry.test")
    before = q.stats()
    res = q.requeue_failed(["http_403"], dry_run=True)
    assert res["matched"] == 1 and res["requeued"] == 0
    assert q.stats() == before


def test_requeue_limit_and_reset_retries(tmp_env):
    q = seed("limit.test")
    res = q.requeue_failed(["http_0"], limit=1)
    assert res["matched"] == 2 and res["requeued"] == 1


def test_requeue_without_patterns_is_noop(tmp_env):
    q = seed("noop.test")
    before = q.stats()
    assert q.requeue_failed([]) == {"matched": 0, "requeued": 0, "by_error": {}}
    assert q.stats() == before
