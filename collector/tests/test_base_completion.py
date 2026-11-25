"""base.collect_urls 收尾语义：发现成功但无新工作 → complete；发现为空 → interrupted。"""
import json

import pytest

from crawler.channels import base as base_mod


class FakeFetcher:
    def stream_requests(self, batch, workers=4):
        return iter(())


def _site(domain="s.test"):
    return {"domain": domain, "channel": "sitemap", "base": "https://s.test"}


def _cursor(status="interrupted"):
    return {"status": status, "saved": 5, "reasons": {}, "next_page": 1, "total_pages": 1}


def test_nothing_new_marks_complete(tmp_env, monkeypatch):
    """发现到 N>0 条 URL（都已入库）、队列无待办 → complete，而不是一直 interrupted。"""
    from crawler.request import RequestQueue
    from crawler.store import append_rows
    append_rows("s.test", [{"url": "https://s.test/a", "title": "saved", "text": "body"}])
    q = RequestQueue("s.test", "posts")
    q.add_many(["https://s.test/a"], channel="sitemap")
    q.mark_done([r.key for r in q.next_batch(10)])

    monkeypatch.setattr(base_mod.cursor, "load", lambda d, k="posts": _cursor())
    saved = {}
    monkeypatch.setattr(base_mod.cursor, "save",
                        lambda d, k="posts", **f: (saved.update(f) or f))
    log = type("L", (), {"info": lambda *a, **k: None})()
    out = base_mod.collect_urls(_site(), [], 0, log, FakeFetcher(),
                                discovered_count=7)
    assert out["status"] == "complete"
    assert saved.get("status") == "complete"
    assert saved.get("last_reason") == "nothing_new"


def test_discovery_empty_marks_interrupted(tmp_env, monkeypatch):
    """发现阶段 0 条 URL（wallis 那种 504/正则失效）→ interrupted，绝不 complete。"""
    monkeypatch.setattr(base_mod.cursor, "load", lambda d, k="posts": _cursor())
    saved = {}
    monkeypatch.setattr(base_mod.cursor, "save",
                        lambda d, k="posts", **f: (saved.update(f) or f))
    log = type("L", (), {"info": lambda *a, **k: None})()
    out = base_mod.collect_urls(_site(), [], 0, log, FakeFetcher(),
                                discovered_count=0)
    assert out["status"] == "interrupted"
    assert saved.get("status") == "interrupted"
    assert saved.get("last_reason") == "discovery_empty"


def test_previous_partial_not_upgraded(tmp_env, monkeypatch):
    """之前已确认 partial（有缺量）的站，不会被'无新工作'升级成 complete。"""
    monkeypatch.setattr(base_mod.cursor, "load", lambda d, k="posts": _cursor("partial"))
    saved = {}
    monkeypatch.setattr(base_mod.cursor, "save",
                        lambda d, k="posts", **f: (saved.update(f) or f))
    log = type("L", (), {"info": lambda *a, **k: None})()
    out = base_mod.collect_urls(_site(), [], 0, log, FakeFetcher(),
                                discovered_count=3)
    assert out["status"] == "partial"


def test_recovered_discovery_clears_stale_partial_without_duplicate_rows(tmp_env):
    from crawler.store import append_rows

    url = "https://s.test/a"
    append_rows("s.test", [{"url": url, "title": "Saved"}])
    base_mod.cursor.save("s.test", status="partial", last_reason="discovery_failed")
    log = type("L", (), {"info": lambda *a, **k: None})()
    result = base_mod.collect_urls(_site(), [url], 0, log, FakeFetcher())
    assert result["status"] == "complete" and result["saved"] == 0
    assert base_mod.cursor.load("s.test")["last_reason"] == "nothing_new"


def test_recovered_discovery_keeps_existing_skipped_items_visible(tmp_env):
    from crawler.request import RequestQueue, canonical_key

    url = "https://s.test/skipped"
    q = RequestQueue("s.test")
    q.add_many([url])
    q.mark_skipped([canonical_key(url)], "no_text")
    base_mod.cursor.save("s.test", status="partial", last_reason="discovery_failed")
    log = type("L", (), {"info": lambda *a, **k: None})()
    result = base_mod.collect_urls(_site(), [url], 0, log, FakeFetcher())
    assert result["status"] == "partial" and result["last_reason"] == "skipped"
    q.conn.close()


def _queue_with_error(tmp_env, domain="s.test", err="http_404 not found"):
    from crawler.request import RequestQueue
    q = RequestQueue(domain, "posts")
    q.add_many(["https://s.test/gone"], channel="sitemap")
    req = q.next_batch(10)[0]
    for _ in range(4):
        q.mark_error([req.key], {req.key: err})
    return q


def test_dead_404_counts_as_complete(tmp_env, monkeypatch):
    """终态错误全是 404/410（页面已删除）→ complete，而不是永远 partial。"""
    _queue_with_error(tmp_env)
    monkeypatch.setattr(base_mod.cursor, "load", lambda d, k="posts": _cursor("partial"))
    saved = {}
    monkeypatch.setattr(base_mod.cursor, "save",
                        lambda d, k="posts", **f: (saved.update(f) or f))
    log = type("L", (), {"info": lambda *a, **k: None})()
    out = base_mod.collect_urls(_site(), [], 0, log, FakeFetcher(), discovered_count=1)
    assert out["status"] == "complete"
    assert saved.get("last_reason") == "dead_404"


def test_dead_429_stays_partial(tmp_env, monkeypatch):
    """终态错误是 429/5xx（限流/故障）→ 保留 partial，提示还需重试。"""
    _queue_with_error(tmp_env, err="http_429 Too Many Requests")
    monkeypatch.setattr(base_mod.cursor, "load", lambda d, k="posts": _cursor("partial"))
    saved = {}
    monkeypatch.setattr(base_mod.cursor, "save",
                        lambda d, k="posts", **f: (saved.update(f) or f))
    log = type("L", (), {"info": lambda *a, **k: None})()
    out = base_mod.collect_urls(_site(), [], 0, log, FakeFetcher(), discovered_count=1)
    assert out["status"] == "partial"


def test_empty_fetcher_does_not_loop_forever(tmp_env):
    log = type('L', (), {'info': lambda *a, **k: None})()
    result = base_mod.collect_urls(_site(), ['https://s.test/missing'], 0, log, FakeFetcher())
    assert result['status'] == 'partial'
    assert result['queue']['error'] == 1
    assert result['saved'] == 0
