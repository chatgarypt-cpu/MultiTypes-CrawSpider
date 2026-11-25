"""Shared helpers."""
import fcntl, html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, threading, time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests, trafilatura
from crawler.config import ROOT, DATA, DB, UA, COLS, MEDIA_COLS, WORKERS
from crawler.request import canonical_key


class CollectionBusy(RuntimeError):
    pass


_HELD = threading.local()


@contextmanager
def collection_lock(domain, kind="posts", *, wait=True, data_dir=None):
    """Serialize a collection across processes, with nesting in the current thread."""
    directory = os.path.realpath(os.path.join(data_dir or DATA, domain))
    os.makedirs(directory, exist_ok=True)
    key = os.getpid(), os.path.join(directory, f".{kind}.lock")
    held = _HELD.paths if hasattr(_HELD, "paths") else set()
    _HELD.paths = held
    if key in held:
        yield
        return
    with open(key[1], "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise CollectionBusy(f"{domain}:{kind} is already being collected") from exc
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)


def _paths(domain, kind="posts"):
    d = os.path.join(DATA, domain); os.makedirs(d, exist_ok=True)
    if kind == "posts":
        return (os.path.join(d, "articles.jsonl"), os.path.join(d, "urls.txt"), os.path.join(d, "cursor.json"))
    return (os.path.join(d, f"{kind}.jsonl"), os.path.join(d, f"{kind}.urls.txt"), os.path.join(d, f"{kind}.cursor.json"))

def load_cursor(domain, kind="posts"):
    _, _, cp = _paths(domain, kind)
    if os.path.exists(cp):
        try: return json.load(open(cp))
        except Exception: pass
    return {"next_page": 1, "total_pages": None, "saved": 0, "done": False, "updated": ""}

def save_cursor(domain, cur, kind="posts"):
    with collection_lock(domain, kind):
        _, _, cp = _paths(domain, kind)
        cur["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tmp = cp + ".tmp"
        with open(tmp, "w") as f: json.dump(cur, f)
        os.replace(tmp, cp)

def load_known(domain, kind="posts"):
    ap, up, _ = _paths(domain, kind)
    with collection_lock(domain, kind):
        _recover_append(ap, up)
        if not os.path.exists(up):
            return set()
        with open(up, encoding="utf-8") as handle:
            return {line.strip() for line in handle if line.strip()}


# ---------- append with dedupe ----------
# urls.txt is the per-(domain, kind) URL registry: the source of truth for "already
# collected". append_rows used to append blindly, so a rerun or a queue reset stored
# the same article twice (lnc: 16,150 dup rows, matangitonga: 2,768). Keep one canonical
# key per line here, cached in-process, and re-read the file only when its stat says it
# changed (another process appended, or dedupe.py rewrote it).

_KNOWN = {}                       # urls.txt path -> {"sig": (size, mtime_ns) | None, "keys": set}


def _stat_sig(path):
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _read_keys(path):
    """Canonical keys of every URL already on disk (dedupe by canonical_key)."""
    keys = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                url = line.strip()
                if url:
                    keys.add(canonical_key(url))
    except OSError:
        return set()
    return keys


def _known_entity(path):
    """Cached key set for `path`, revalidated by (size, mtime) on every call."""
    sig = _stat_sig(path)
    ent = _KNOWN.get(path)
    if ent is None or ent["sig"] != sig:
        ent = {"sig": sig, "keys": _read_keys(path)}
        _KNOWN[path] = ent
    return ent


def _append_lines(path, lines):
    """追加写一组行并 flush；出错时由调用方回滚到原文件长度。"""
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line)
        f.flush()
        os.fsync(f.fileno())


def _size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def _sync_parent(path):
    descriptor = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _recover_append(articles, urls, *, rollback=False):
    """Finish or undo the only pending append while holding the collection lock."""
    journal = urls + ".append"
    try:
        with open(journal, encoding="utf-8") as handle:
            pending = json.load(handle)
    except FileNotFoundError:
        return
    paths = (articles, urls)
    sizes = [_size(path) for path in paths]
    if any(size < start or size > end for size, start, end in
           zip(sizes, pending["start"], pending["end"], strict=True)):
        raise OSError(f"Collection files changed outside the append transaction: {journal}")
    if rollback or sizes != pending["end"]:
        for path, size, start in zip(paths, sizes, pending["start"], strict=True):
            if size != start:
                with open(path, "r+b") as handle:
                    handle.truncate(start)
                    os.fsync(handle.fileno())
        _KNOWN.pop(urls, None)
    os.unlink(journal)


def append_rows(domain, rows, kind="posts"):
    """Append rows to the store, skipping URLs we already have (same domain+kind).

    Returns the number of rows actually written, so callers can report real progress
    (and so a duplicate batch is visibly 0 instead of inflating the counters).

    articles.jsonl 与 urls.txt 必须同进同退：任一步写入失败就把两个文件都回滚到
    写入前长度后抛异常。调用方（channels/base.py）只有在本函数成功返回后才把队列
    行标 done，所以"写盘失败 → 行仍是 unprocessed → 下次重跑补齐"，也不会出现
    "urls.txt 有记录但正文缺失"导致重跑被去重跳过（2026-11 审查问题 1）。
    """
    ap, up, _ = _paths(domain, kind)
    cols = MEDIA_COLS if kind == "media" else COLS
    with collection_lock(domain, kind):
        _recover_append(ap, up)
        keys = _known_entity(up)["keys"]
        fresh = []
        new_keys = set()
        for r in rows:
            url = (r.get("url") or "").strip()
            key = canonical_key(url) if url else ""
            if not key or key in keys or key in new_keys:
                continue
            new_keys.add(key)
            fresh.append(r)
        if not fresh:
            return 0
        lines = (
            [json.dumps({k: r.get(k, "") for k in cols}, ensure_ascii=False) + "\n" for r in fresh],
            [(r.get("url") or "").strip() + "\n" for r in fresh],
        )
        start = [_size(path) for path in (ap, up)]
        end = [size + sum(len(line.encode("utf-8")) for line in block)
               for size, block in zip(start, lines, strict=True)]
        journal = up + ".append"
        temp = journal + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"start": start, "end": end}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, journal)
        _sync_parent(journal)
        try:
            _append_lines(ap, lines[0])
            _append_lines(up, lines[1])
        except Exception:
            _recover_append(ap, up, rollback=True)
            raise
        os.unlink(journal)
        # Our own append changed the stat; record it so the next call does not reload.
        keys.update(new_keys)
        _KNOWN[up]["sig"] = _stat_sig(up)
        return len(fresh)


# ---------- state ----------
def db():
    os.makedirs(DATA, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, channel TEXT, started TEXT, finished TEXT,
        discovered INTEGER, saved INTEGER, errors INTEGER, status TEXT, note TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS runs_finished ON runs(finished)")
    return c

def existing_urls(domain):
    p = os.path.join(DATA, domain, "articles.jsonl")
    if not os.path.exists(p): return set(), 0
    urls, n = set(), 0
    for line in open(p, encoding="utf-8"):
        try: urls.add(json.loads(line)["url"]); n += 1
        except Exception: pass
    return urls, n

def save(domain, rows):
    append_rows(domain, rows)
