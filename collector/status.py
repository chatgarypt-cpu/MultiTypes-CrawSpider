#!/usr/bin/env python3
"""Write status.json + a self-refreshing dashboard.html from data/*/cursor.json."""
import json, os, glob, re, subprocess
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")

def running():
    try:
        out = subprocess.run(["pgrep", "-f", "collect.py --"], capture_output=True, text=True)
        return bool(out.stdout.strip())
    except Exception:
        return False

def queue_running():
    try:
        out = subprocess.run(["pgrep", "-f", "run_queue.py"], capture_output=True, text=True)
        return bool(out.stdout.strip())
    except Exception:
        return False

def tasks_running():
    try:
        out = subprocess.run(["pgrep", "-f", "collect.py --site"], capture_output=True, text=True)
        return len([l for l in out.stdout.splitlines() if l.strip()])
    except Exception:
        return 0

def running_domains():
    out = subprocess.run(["pgrep", "-f", "collect.py --site"], capture_output=True, text=True)
    pids = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    if not pids: return []
    ps = subprocess.run(["ps", "-o", "command=", "-p", ",".join(pids)], capture_output=True, text=True)
    return sorted({m.group(1) for m in re.finditer(r"--site (\S+)", ps.stdout)})

def blocked_domains():
    try:
        return sorted({x.split(":")[0] for x in json.load(open(os.path.join(DATA, "queue_blocked.json")))})
    except Exception:
        return []

def concurrency():
    try:
        return int(json.load(open(os.path.join(DATA, "queue_config.json"))).get("concurrency", 4))
    except Exception:
        return 4

_COUNT_CACHE = {}


def count_lines(path):
    """行数计数带 mtime/size 缓存——大文件（几十万行）不必每次刷新都重扫。"""
    try:
        st = os.stat(path)
    except OSError:
        return 0
    key = (st.st_mtime_ns, st.st_size)
    hit = _COUNT_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, "rb") as f:
            n = sum(1 for _ in f)
    except OSError:
        return 0
    _COUNT_CACHE[path] = (key, n)
    return n


def site_row(d):
    cfg = source_cfg().get(os.path.basename(d), {})
    os.makedirs(d, exist_ok=True)
    dom = os.path.basename(d)
    cur = {}
    cp = os.path.join(d, "cursor.json")
    if os.path.exists(cp):
        try: cur = json.load(open(cp))
        except Exception: pass
    n = 0; chars = 0
    up = os.path.join(d, "urls.txt")
    if os.path.exists(up): n = count_lines(up)
    ap = os.path.join(d, "articles.jsonl")
    if os.path.exists(ap): chars = os.path.getsize(ap)
    def kind_count(kind):
        f = os.path.join(d, "pages.urls.txt" if kind == "pages" else "media.urls.txt")
        c = os.path.join(d, "pages.cursor.json" if kind == "pages" else "media.cursor.json")
        n = count_lines(f) if os.path.exists(f) else 0
        cur2 = {}
        if os.path.exists(c):
            try: cur2 = json.load(open(c))
            except Exception: pass
        return {"n": n, "page": cur2.get("next_page", 0), "pages": cur2.get("total_pages"),
                "done": cur2.get("done", False), "updated": (cur2.get("updated") or "")[:19]}
    return {"domain": dom, "saved": n,
            "page": cur.get("next_page", 0), "pages": cur.get("total_pages"),
            "posts": cur.get("total_posts"), "done": cur.get("done", False),
            "updated": (cur.get("updated") or "")[:19], "mb": round(chars / 1e6, 2),
            "pages_n": kind_count("pages")["n"], "media_n": kind_count("media")["n"],
            "channel": cfg.get("channel", ""), "needs_config": bool(cfg.get("channel") == "html" and not cfg.get("list_url")),
            "kinds": {"posts": {"n": n, "page": cur.get("next_page", 0), "pages": cur.get("total_pages"), "updated": (cur.get("updated") or "")[:19]},
                      "pages": kind_count("pages"), "media": kind_count("media")}}

_last = {"total": None, "at": 0.0, "rate": 0.0}

def source_cfg():
    try:
        import yaml
        return {x["domain"]: x for x in yaml.safe_load(open(os.path.join(ROOT, "sources.yaml")))}
    except Exception:
        return {}

def all_domains():
    """Every configured site, not just the ones that already produced rows."""
    try:
        import yaml
        sites = yaml.safe_load(open(os.path.join(ROOT, "sources.yaml")))
        doms = [s["domain"] for s in sites]
    except Exception:
        doms = []
    on_disk = frozenset(os.path.basename(d) for d in glob.glob(os.path.join(DATA, "*"))
                        if os.path.isdir(d) and not os.path.islink(d))
    return sorted(set(doms) | set(on_disk))

def build_status():
    import time as _t
    global _last
    rows = [site_row(os.path.join(DATA, d)) for d in all_domains()]
    rows.sort(key=lambda r: -r["saved"])
    total = sum(r["saved"] for r in rows)
    now = _t.time()
    if _last["total"] is None:
        _last.update(total=total, at=now)
    elif now - _last["at"] >= 1.0:          # one window regardless of how many clients poll
        if total >= _last["total"]:
            _last["rate"] = (total - _last["total"]) / (now - _last["at"])
        _last.update(total=total, at=now)
    if _last["rate"] > 0:
        _last["shown"], _last["shown_at"] = _last["rate"], now
    stale = now - _last.get("shown_at", 0) > 20
    rate = 0 if stale else _last.get("shown", 0)
    return {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rate": round(rate, 1),
            "running": running(), "queue_running": queue_running(), "tasks_running": tasks_running(),
            "running_domains": running_domains(), "blocked": blocked_domains(),
            "concurrency": concurrency(), "total": total, "sites": rows}

def main():
    state = build_status()
    json.dump(state, open(os.path.join(DATA, "status.json"), "w"), ensure_ascii=False)

if __name__ == "__main__":
    main()
