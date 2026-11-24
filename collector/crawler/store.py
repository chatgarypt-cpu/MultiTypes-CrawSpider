"""Shared helpers."""
import html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests, trafilatura
from crawler.config import ROOT, DATA, DB, UA, COLS, MEDIA_COLS, WORKERS
from crawler.request import canonical_key

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
    _, _, cp = _paths(domain, kind)
    cur["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = cp + ".tmp"
    with open(tmp, "w") as f: json.dump(cur, f)
    os.replace(tmp, cp)          # atomic: never leave a half-written cursor

def load_known(domain, kind="posts"):
    _, up, _ = _paths(domain, kind)
    if not os.path.exists(up): return set()
    return {l.strip() for l in open(up, encoding="utf-8") if l.strip()}


# ---------- append with dedupe ----------
# urls.txt is the per-(domain, kind) URL registry: the source of truth for "already
# collected". append_rows used to append blindly, so a rerun or a queue reset stored
# the same article twice (lnc: 16,150 dup rows, matangitonga: 2,768). Keep one canonical
# key per line here, cached in-process, and re-read the file only when its stat says it
# changed (another process appended, or dedupe.py rewrote it).

_KNOWN = {}                       # urls.txt path -> {"sig": (size, mtime_ns) | None, "keys": set}
_KNOWN_LOCK = threading.RLock()


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
    with _KNOWN_LOCK:
        keys = _known_entity(up)["keys"]
        fresh = []
        for r in rows:
            url = (r.get("url") or "").strip()
            key = canonical_key(url) if url else ""
            if not key or key in keys:
                continue
            keys.add(key)
            fresh.append(r)
        if not fresh:
            return 0
        ap_size = os.path.getsize(ap) if os.path.exists(ap) else 0
        up_size = os.path.getsize(up) if os.path.exists(up) else 0
        try:
            _append_lines(ap, [json.dumps({k: r.get(k, "") for k in cols}, ensure_ascii=False) + "\n"
                               for r in fresh])
            _append_lines(up, [(r.get("url") or "").strip() + "\n" for r in fresh])
        except Exception:
            for path, size in ((ap, ap_size), (up, up_size)):
                try:
                    os.truncate(path, size)
                except OSError:
                    pass
            _KNOWN.pop(up, None)          # 内存里的 known 缓存已提前加过 key，弃掉重读
            raise
        # Our own append changed the stat; record it so the next call does not reload.
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
    d = os.path.join(DATA, domain); os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "articles.jsonl"), "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({k: r.get(k, "") for k in COLS}, ensure_ascii=False) + "\n")
