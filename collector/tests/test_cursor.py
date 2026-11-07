"""cursor 状态机单测（离线，tmp_path 隔离）。

覆盖规格 2.2：status 取值、done == (status=="complete")、旧 JSON 兼容、
mark_* 状态迁移、reasons 计数、旧 save_cursor 签名、原子写。
"""
import json
from pathlib import Path

from crawler import cursor


def _disk(tmp_env, domain, kind="posts"):
    name = "cursor.json" if kind == "posts" else f"{kind}.cursor.json"
    return json.loads((Path(tmp_env) / domain / name).read_text(encoding="utf-8"))


def test_fresh_cursor_is_not_complete(tmp_env):
    cur = cursor.load("a.test")
    assert cur["status"] in cursor.STATUS
    assert cur["status"] == "interrupted"
    assert cur["done"] is False
    assert cur["next_page"] == 1 and cur["saved"] == 0
    assert cursor.is_complete("a.test") is False


def test_mark_complete_sets_done_and_roundtrips(tmp_env):
    cur = cursor.mark_complete("a.test", total_pages=3, next_page=4, saved=10)
    assert cur["status"] == "complete" and cur["done"] is True
    raw = _disk(tmp_env, "a.test")
    assert raw["status"] == "complete" and raw["done"] is True
    assert raw["next_page"] == 4 and raw["saved"] == 10
    assert raw["updated"]
    assert cursor.is_complete("a.test") is True
    assert cursor.load("a.test")["status"] == "complete"


def test_legacy_json_without_status_derives_from_done(tmp_env):
    d = Path(tmp_env) / "old.test"
    d.mkdir()
    (d / "cursor.json").write_text(json.dumps({"next_page": 5, "done": True, "saved": 7}))
    cur = cursor.load("old.test")
    assert cur["status"] == "complete" and cur["done"] is True
    assert cur["saved"] == 7 and cur["next_page"] == 5

    (d / "media.cursor.json").write_text(json.dumps({"next_page": 2, "done": False}))
    media = cursor.load("old.test", "media")
    assert media["status"] == "interrupted" and media["done"] is False
    assert not cursor.is_complete("old.test", "media")


def test_inconsistent_status_and_done_prefers_done(tmp_env):
    # repair.py 这类旧工具只改 done；不能还留着 complete
    d = Path(tmp_env) / "e.test"
    d.mkdir()
    (d / "cursor.json").write_text(json.dumps({"status": "complete", "done": False}))
    assert cursor.load("e.test")["status"] == "interrupted"
    assert cursor.is_complete("e.test") is False


def test_partial_accumulates_reasons_and_persists(tmp_env):
    cursor.mark_partial("p.test", "posts", reason="empty_page", reasons={"http_404": 2}, next_page=7)
    cur = cursor.load("p.test")
    assert cur["status"] == "partial" and cur["done"] is False
    assert cur["reasons"] == {"empty_page": 1, "http_404": 2}
    assert cur["last_reason"] == "empty_page"
    assert cur["next_page"] == 7
    assert not cursor.is_complete("p.test")

    cursor.mark_partial("p.test", "posts", reason="empty_page")   # 再记一次同一个原因
    assert cursor.load("p.test")["reasons"]["empty_page"] == 2


def test_blocked_and_paused(tmp_env):
    cur = cursor.mark_blocked("b.test", "posts", reason="http_403")
    assert cur["status"] == "blocked" and cur["done"] is False
    assert cur["blocked_reason"] == "http_403"
    assert cursor.load("b.test")["reasons"]["http_403"] == 1
    assert cursor.is_complete("b.test") is False

    cur = cursor.mark_paused("b.test", "posts", reason="limit")
    assert cur["status"] == "paused" and cur["done"] is False
    assert cur["last_reason"] == "limit"


def test_legacy_save_cursor_signature_and_kind_paths(tmp_env):
    out = cursor.save_cursor("c.test", {"next_page": 3, "total_pages": 9,
                                        "saved": 42, "done": True}, "media")
    assert out["status"] == "complete" and out["done"] is True
    path = Path(tmp_env) / "c.test" / "media.cursor.json"
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["next_page"] == 3 and raw["saved"] == 42 and raw["done"] is True
    assert cursor.load("c.test", "media")["status"] == "complete"


def test_save_is_atomic_and_keeps_old_fields(tmp_env):
    cursor.save("d.test", next_page=2, total_pages=8, saved=5, custom_field="keep")
    cur = cursor.load("d.test")
    assert cur["total_pages"] == 8 and cur["next_page"] == 2 and cur["saved"] == 5
    assert cur["custom_field"] == "keep"               # 未知字段原样保留
    assert not any(p.name.endswith(".tmp") for p in (Path(tmp_env) / "d.test").iterdir())
