"""Exercise URL deduplication, cache invalidation and historical row repair."""
import json
import time

import dedupe as dedupe_mod

from crawler import store
from crawler.request import RequestQueue, canonical_key


def _rows(*urls):
    return [{"url": u, "title": f"t{i}", "text": "body"} for i, u in enumerate(urls)]


def _lines(path):
    return [line for line in open(path, encoding="utf-8") if line.strip()]


# ---------- append_rows 去重 ----------

def test_append_rows_dedupes_same_batch(tmp_env):
    urls = [
        "https://dup.test/a",
        "https://dup.test/a/",                 # 尾斜杠 → 同一 canonical key
        "https://dup.test/a?utm_source=x",     # tracking 参数 → 同一
        "https://dup.test/b",
        "https://DUP.test/b",                  # 主机名大小写 → 同一
    ]
    written = store.append_rows("dup.test", _rows(*urls))
    assert written == 2
    assert len(_lines(tmp_env / "dup.test" / "articles.jsonl")) == 2
    assert len(_lines(tmp_env / "dup.test" / "urls.txt")) == 2


def test_append_rows_rerun_is_a_noop(tmp_env):
    """事故回归：整站重抓 N 遍，库里不能出现重复行。"""
    rows = _rows(*[f"https://accident.test/post/{i}" for i in range(20)])
    assert store.append_rows("accident.test", rows) == 20
    for _ in range(5):
        assert store.append_rows("accident.test", rows) == 0
    assert len(_lines(tmp_env / "accident.test" / "articles.jsonl")) == 20
    assert len(_lines(tmp_env / "accident.test" / "urls.txt")) == 20


def test_append_rows_returns_actual_written_count(tmp_env):
    store.append_rows("count.test", _rows("https://count.test/a"))
    n = store.append_rows("count.test", _rows(
        "https://count.test/a", "https://count.test/b", "https://count.test/c"))
    assert n == 2
    assert len(_lines(tmp_env / "count.test" / "urls.txt")) == 3


def test_media_kind_has_its_own_files_and_cache(tmp_env):
    rows = [{"url": "https://media.test/a.jpg", "id": 1},
            {"url": "https://media.test/a.jpg", "id": 2}]
    assert store.append_rows("media.test", rows, "media") == 1
    assert (tmp_env / "media.test" / "media.jsonl").exists()
    assert (tmp_env / "media.test" / "media.urls.txt").exists()
    # posts 与 media 的 known 集合互不影响
    assert store.append_rows("media.test", _rows("https://media.test/a.jpg")) == 1


# ---------- 进程内 known 缓存 ----------

def test_cache_not_reloaded_when_file_unchanged(tmp_env, monkeypatch):
    calls = []
    real = store._read_keys

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(store, "_read_keys", counting)
    for i in range(3):
        store.append_rows("cache.test", _rows(f"https://cache.test/{i}"))
    assert len(calls) == 1          # 首次懒加载，后续命中缓存


def test_external_append_invalidates_cache(tmp_env):
    store.append_rows("append.test", _rows("https://append.test/1"))
    with open(tmp_env / "append.test" / "urls.txt", "a", encoding="utf-8") as fh:
        fh.write("https://append.test/2\n")          # 另一个进程写入
    assert store.append_rows("append.test", _rows("https://append.test/2")) == 0


def test_external_rewrite_invalidates_cache(tmp_env):
    store.append_rows("rewrite.test", _rows(
        "https://rewrite.test/1", "https://rewrite.test/2"))
    (tmp_env / "rewrite.test" / "urls.txt").write_text("https://rewrite.test/1\n", encoding="utf-8")
    # 文件被重写后 /2 不在 known 里了 → 应重新写入
    assert store.append_rows("rewrite.test", _rows("https://rewrite.test/2")) == 1


def test_dedupe_rewrite_is_seen_by_append_cache(tmp_env):
    domain = "dedupe.test"
    d = tmp_env / domain
    d.mkdir(parents=True)
    rows = [
        {"url": "https://dedupe.test/a", "title": "short", "text": "x"},
        {"url": "https://dedupe.test/a", "title": "long", "text": "x" * 500},
        {"url": "https://dedupe.test/b", "title": "b", "text": "y"},
    ]
    (d / "articles.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (d / "urls.txt").write_text(
        "https://dedupe.test/a\nhttps://dedupe.test/a\nhttps://dedupe.test/b\n", encoding="utf-8")
    # 预热 append 缓存（读的是重写前的 urls.txt）
    assert store.append_rows(domain, _rows("https://dedupe.test/c")) == 1

    stats = dedupe_mod.dedupe(domain)
    assert stats["removed"] == 1 and stats["after"] == 3   # a 的两行合一；b、c 保留
    # 去重后 /a 仍在 urls.txt → append 不应重复写；改过的 size/mtime 必须被感知
    assert store.append_rows(domain, _rows("https://dedupe.test/a")) == 0


def test_dedupe_dry_run_writes_nothing(tmp_env):
    domain = "dry.test"
    d = tmp_env / domain
    d.mkdir(parents=True)
    articles = d / "articles.jsonl"
    urls = d / "urls.txt"
    articles.write_text(
        json.dumps({"url": "https://dry.test/a", "text": "1"}) + "\n"
        + json.dumps({"url": "https://dry.test/a", "text": "22"}) + "\n", encoding="utf-8")
    urls.write_text("https://dry.test/a\nhttps://dry.test/a\n", encoding="utf-8")
    before_a, before_u = articles.read_text(encoding="utf-8"), urls.read_text(encoding="utf-8")

    stats = dedupe_mod.dedupe(domain, dry_run=True)

    assert stats["dry_run"] is True and stats["removed"] == 1
    assert articles.read_text(encoding="utf-8") == before_a      # 一个字节都不许动
    assert urls.read_text(encoding="utf-8") == before_u


def test_load_known_keeps_raw_urls(tmp_env):
    d = tmp_env / "known.test"
    d.mkdir(parents=True)
    (d / "urls.txt").write_text(
        "https://known.test/a?utm_source=x\nhttps://known.test/b\n", encoding="utf-8")
    assert store.load_known("known.test") == {
        "https://known.test/a?utm_source=x", "https://known.test/b"}


def test_append_rows_100k_cache_is_fast(tmp_env):
    domain = "perf.test"
    d = tmp_env / domain
    d.mkdir(parents=True)
    with open(d / "urls.txt", "w", encoding="utf-8") as fh:
        fh.writelines(f"https://perf.test/p/{i}\n" for i in range(100_000))

    rows = _rows(*[f"https://perf.test/p/{i}" for i in range(0, 100_000, 100)])  # 1000 条重复
    rows += _rows(*[f"https://perf.test/new/{i}" for i in range(200)])           # 200 条新增

    t0 = time.perf_counter()
    written = store.append_rows(domain, rows)
    first = time.perf_counter() - t0

    assert written == 200
    assert first < 2.0                      # 10 万行首次加载 + 混合写入

    t0 = time.perf_counter()
    assert store.append_rows(domain, _rows("https://perf.test/new/0")) == 0
    assert time.perf_counter() - t0 < 0.5   # 缓存命中后不应重读全文件


# ---------- RequestQueue：WAL / busy_timeout + mark_error 影响面 ----------

def test_request_queue_pragmas(tmp_env):
    q = RequestQueue("wal.test")
    assert q.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert q.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    assert q.conn.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL


def test_mark_error_only_touches_given_keys(tmp_env):
    """事故回归：mark_error 曾整站 UPDATE，把 done 行重置成 unprocessed。"""
    q = RequestQueue("err.test")
    q.add_many([f"https://err.test/p/{i}" for i in range(5)])
    keys = [r.key for r in q.next_batch(10)]
    q.mark_done(keys[:3])

    q.mark_error([keys[3]], {keys[3]: "timeout"})

    assert q.stats() == {"done": 3, "unprocessed": 2}
    row = q.conn.execute(
        "SELECT state, retries, error FROM requests WHERE domain=? AND key=?",
        ("err.test", keys[3])).fetchone()
    assert row == ("unprocessed", 1, "timeout")
    # 未传进来的 done 行状态、重试计数都没被碰
    assert q.conn.execute(
        "SELECT COUNT(*) FROM requests WHERE domain=? AND key IN (?,?,?) AND state='done'",
        ("err.test", *keys[:3])).fetchone()[0] == 3


def test_mark_error_stops_at_max_retries(tmp_env):
    q = RequestQueue("retry.test")
    q.add_many(["https://retry.test/a"])
    key = q.next_batch(1)[0].key
    for _ in range(3):
        q.mark_error([key], {key: "boom"})
    assert q.conn.execute(
        "SELECT state, retries FROM requests WHERE key=?", (key,)).fetchone() == ("error", 3)
