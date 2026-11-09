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
