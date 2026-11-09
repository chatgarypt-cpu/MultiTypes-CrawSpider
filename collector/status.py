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
    # 沙箱/极简容器里 ps 可能被禁：进程探测失败不能让整个状态流挂掉，降级为"不知道在跑谁"
    try:
        out = subprocess.run(["pgrep", "-f", "collect.py --site"], capture_output=True, text=True)
        pids = [l.strip() for l in out.stdout.splitlines() if l.strip()]
        if not pids: return []
        ps = subprocess.run(["ps", "-o", "command=", "-p", ",".join(pids)], capture_output=True, text=True)
        return sorted({m.group(1) for m in re.finditer(r"--site (\S+)", ps.stdout)})
    except Exception:
        return []

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
_COVERAGE_CACHE = {"sig": None, "map": {}}
# verify_coverage.py 的口径：coverage >= 1.5（本地远大于上游）视为口径异常，不算核对过
COVERAGE_ANOMALY = 1.5


def latest_coverage():
    """最新 data/coverage-*.json 的 {domain: record}（只读，无快照/坏文件都返回 {}）。

    覆盖率快照不会每秒变，用 (path, mtime, size) 缓存解析结果。
    """
    try:
        cands = sorted(glob.glob(os.path.join(DATA, "coverage-*.json")),
                       key=lambda p: os.stat(p).st_mtime, reverse=True)
    except OSError:
        return {}
    if not cands:
        return {}
    try:
        st = os.stat(cands[0])
        sig = (cands[0], st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _COVERAGE_CACHE.get("sig") == sig:
        return _COVERAGE_CACHE["map"]
    out = {}
    try:
        with open(cands[0], encoding="utf-8") as f:
            payload = json.load(f)
        for rec in (payload.get("report") or []) if isinstance(payload, dict) else []:
            if isinstance(rec, dict) and rec.get("domain"):
                out[rec["domain"]] = rec
    except Exception:
        out = {}
    _COVERAGE_CACHE.update(sig=sig, map=out)
    return out


def coverage_flags(dom):
    """每站覆盖率三类判定：已核对（>=95% 或不可验证）/ 未核对 / 口径异常（旧快照也按同一阈值兜底）。"""
    rec = latest_coverage().get(dom)
    if not rec:
        return {"coverage": None, "verified": False, "coverage_verdict": "未核对"}
    cov = rec.get("coverage")
    verdict = rec.get("verdict") or ""
    source = str(rec.get("source") or "")
    if not verdict:                              # 旧快照没有 verdict 字段，按同一口径推断
        if isinstance(cov, (int, float)) and cov >= COVERAGE_ANOMALY:
            verdict = "口径异常"
        elif cov is None and source.startswith("unverifiable"):
            verdict = "不可验证"
    if verdict == "口径异常" or (isinstance(cov, (int, float)) and cov >= COVERAGE_ANOMALY):
        return {"coverage": None, "verified": False, "coverage_verdict": "口径异常"}
    if verdict == "不可验证":
        return {"coverage": None, "verified": True, "coverage_verdict": "不可验证"}
    if isinstance(cov, (int, float)) and cov >= 0.95:
        return {"coverage": cov, "verified": True, "coverage_verdict": "已核对"}
    if isinstance(cov, (int, float)):
        return {"coverage": cov, "verified": False, "coverage_verdict": "覆盖率不足"}
    return {"coverage": None, "verified": False, "coverage_verdict": "未核对"}


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
    disabled = cfg.get("disabled") is True
    cov = coverage_flags(dom)
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
            "channel": cfg.get("channel", ""),
            # 三类必须分开：任务结束（done，前端另有"不完整"判定）、覆盖率已核对（verified）、
            # 缺入口或不可访问（needs_config / disabled）。disabled 站不再算"待配置"。
            "coverage": cov["coverage"], "verified": cov["verified"],
            "coverage_verdict": cov["coverage_verdict"],
            "needs_config": bool(not disabled and cfg.get("channel") == "html"
                                 and not (cfg.get("list_url") or cfg.get("list_urls"))),
            "disabled": disabled, "disabled_reason": cfg.get("disabled_reason", ""),
            "kinds": {"posts": {"n": n, "page": cur.get("next_page", 0), "pages": cur.get("total_pages"),
                                # done/status 必须带上：前端"不完整"判定依赖每类的 done，
                                # 之前 posts 缺 done 导致 30 个已完成的非 WP 站被误判为不完整
                                "done": cur.get("done", False), "status": cur.get("status", ""),
                                "updated": (cur.get("updated") or "")[:19]},
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
    # 只算"像站点"的数据目录（jobs/recon 之类运行目录、测试站 blog.python.org 不算）
    site_files = ("cursor.json", "urls.txt", "articles.jsonl", "pages.cursor.json", "media.cursor.json")
    on_disk = frozenset(
        os.path.basename(d) for d in glob.glob(os.path.join(DATA, "*"))
        if os.path.isdir(d) and not os.path.islink(d)
        and os.path.basename(d) not in {"blog.python.org"}
        and any(os.path.exists(os.path.join(d, f)) for f in site_files))
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
