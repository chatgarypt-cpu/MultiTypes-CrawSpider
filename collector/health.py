#!/usr/bin/env python3
"""健康快照：给守护进程/监控/看板一份机器可读的运行状态。

  python3 collector/health.py            # 写 data/health.json 并打印 JSON

字段（`health.snapshot()` 返回，`data/health.json` 同构）：
  ts / pid / running / pending / stalled / done / paused /
  total_rows / rate_5m / errors_1h / last_error
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # collector/
DATA = ROOT / "data"
KINDS = ("posts", "pages", "media")
SITE_MARKERS = ("urls.txt", "articles.jsonl", "pages.jsonl", "media.jsonl",
                "cursor.json", "pages.cursor.json", "media.cursor.json")
RATE_WINDOW_SEC = 300                            # rate_5m 的统计窗口
_COUNT_CACHE: dict[str, tuple] = {}
_LAST: dict = {"ts": 0.0, "total": None}


def _default_data_dir() -> Path:
    try:
        from crawler import config
        return Path(config.DATA)
    except Exception:
        return DATA


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def count_lines(path: Path) -> int:
    """带 mtime/size 缓存的行数统计：health 每 30s 写一次，不能每次全量重扫。"""
    try:
        st = os.stat(path)
    except OSError:
        return 0
    key = (st.st_mtime_ns, st.st_size)
    hit = _COUNT_CACHE.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, "rb") as f:
            n = sum(1 for _ in f)
    except OSError:
        return 0
    _COUNT_CACHE[str(path)] = (key, n)
    return n


def _site_lines(data_dir: Path, domain: str, kind: str) -> int:
    d = data_dir / domain
    primary = d / ("urls.txt" if kind == "posts" else f"{kind}.urls.txt")
    fallback = d / ("articles.jsonl" if kind == "posts" else f"{kind}.jsonl")
    return count_lines(primary) if primary.exists() else count_lines(fallback)


def total_rows(data_dir) -> int:
    """所有站点 urls.txt 行数之和（与 status.py / 交付清单同口径）。"""
    data = Path(data_dir)
    if not data.is_dir():
        return 0
    total = 0
    for p in sorted(data.iterdir()):
        if not p.is_dir() or p.is_symlink():
            continue
        if not any((p / m).exists() for m in SITE_MARKERS):
            continue
        from crawler.config import stored_kinds
        total += sum(_site_lines(data, p.name, kind) for kind in stored_kinds(p))
    return total


def compute_rate(prev_total, prev_ts, total, now, window=RATE_WINDOW_SEC):
    """两次快照之间的增量速率（行/分钟）；窗口超时或回退时返回 0。"""
    if prev_total is None or prev_ts is None:
        return 0.0
    elapsed = now - prev_ts
    if elapsed <= 0 or elapsed > window:
        return 0.0
    delta = total - prev_total
    if delta < 0:
        return 0.0
    return round(delta / elapsed * 60, 1)


def _read_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _queue_errors(db_path: Path, now: float) -> tuple[int, str]:
    """最近 1 小时 error 行数 + 最近一条错误信息。"""
    if not db_path.exists():
        return 0, ""
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 3600))
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM requests WHERE state='error' AND updated >= ?",
                (cutoff,)).fetchone()[0]
            row = conn.execute(
                "SELECT error FROM requests WHERE error != '' AND error IS NOT NULL "
                "ORDER BY updated DESC LIMIT 1").fetchone()
            last = (row[0] or "").replace("\n", " ").replace("\r", " ").strip()
            return int(n or 0), last[:300]          # health 只留摘要，别把整页 HTML 塞进去
        finally:
            conn.close()
    except sqlite3.Error:
        return 0, ""


def snapshot(data_dir=None, db_path=None, state_path=None, running=None,
             pending=None, stalled=None, done=None, paused=None) -> dict:
    """生成一份 health 快照。显式传入的字段优先；否则从 supervise 状态/数据目录推导。"""
    data = Path(data_dir) if data_dir else _default_data_dir()
    if state_path is None:
        state = _read_state(data / "supervise_state.json")
    else:
        state = _read_state(Path(state_path))

    if running is None:
        running = [f"{r.get('domain')}:{r.get('kind')}"
                   for r in state.get("running", []) if isinstance(r, dict)]
    if pending is None:
        pending = len(state.get("pending", []) or [])
    if stalled is None:
        stalled = len(state.get("stalled", []) or [])
    if done is None:
        done = len(state.get("completed", []) or [])
    if paused is None:
        paused = (data / "CRAWL_PAUSED").exists()

    now = time.time()
    rows = total_rows(data)
    rate = compute_rate(_LAST["total"], _LAST["ts"], rows, now)
    _LAST.update(ts=now, total=rows)

    db = Path(db_path) if db_path else data / "state.db"
    errors_1h, last_error = _queue_errors(db, now)

    return {
        "ts": now_iso(),
        "pid": os.getpid(),
        "running": list(running),
        "pending": int(pending),
        "stalled": int(stalled),
        "done": int(done),
        "paused": bool(paused),
        "total_rows": rows,
        "rate_5m": rate,
        "errors_1h": errors_1h,
        "last_error": last_error,
    }


def write_health(path=None, **kwargs) -> dict:
    """生成快照并原子写入（默认 data/health.json），返回 payload。"""
    target = Path(path) if path else _default_data_dir() / "health.json"
    payload = snapshot(**kwargs)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, target)
    return payload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="health.py",
                                 description="写 data/health.json 健康快照并打印")
    ap.add_argument("--path", help="输出文件路径（默认 data/health.json）")
    ap.add_argument("--no-write", action="store_true", help="只打印，不写文件")
    args = ap.parse_args(argv)
    if args.no_write:
        payload = snapshot()
    else:
        payload = write_health(args.path)
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
