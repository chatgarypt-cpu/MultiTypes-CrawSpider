#!/usr/bin/env python3
"""Write status.json + a self-refreshing dashboard.html from data/*/cursor.json."""
import json, os, glob, re, subprocess
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
from crawler.config import DATA, REQUEST_QUEUE_CHANNELS, site_kinds, sources_path

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

def _collector_commands():
    import shlex
    try:
        out = subprocess.run(["pgrep", "-f", "collect.py --site"], capture_output=True, text=True)
        pids = out.stdout.split()
        if not pids:
            return []
        ps = subprocess.run(["ps", "-o", "command=", "-p", ",".join(pids)], capture_output=True, text=True)
        commands = []
        for line in ps.stdout.splitlines():
            try:
                parts = shlex.split(line)
            except ValueError:
                continue
            if parts and os.path.basename(parts[0]).startswith("python") and "--site" in parts:
                commands.append(parts)
        return commands
    except OSError:
        return []


def tasks_running():
    return len(_collector_commands())


def running_domains():
    return sorted({args[args.index("--site") + 1] for args in _collector_commands()
                   if args.index("--site") + 1 < len(args)})


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
    """Merge per-site results and historical reports by checked time, never file order alone."""
    paths = set(glob.glob(os.path.join(DATA, "coverage-*.json")))
    paths.update(glob.glob(os.path.join(DATA, "coverage-sites", "*.json")))
    legacy = os.path.join(DATA, "coverage_report.json")
    if os.path.isfile(legacy):
        paths.add(legacy)
    sig = tuple(sorted((p, os.stat(p).st_mtime_ns, os.stat(p).st_size) for p in paths))
    if _COVERAGE_CACHE.get("sig") == sig:
        return _COVERAGE_CACHE["map"]
    out = {}
    for path, mtime, _ in sig:
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                continue
            stamp = payload.get("generated") or datetime.fromtimestamp(mtime / 1e9, timezone.utc).isoformat()
            records = payload.get("report", [payload] if payload.get("domain") else [])
            for record in records:
                if not isinstance(record, dict) or not record.get("domain"):
                    continue
                rec = dict(record, checked_at=record.get("checked_at") or stamp)
                old = out.get(rec["domain"])
                if not old or _epoch(rec["checked_at"]) >= _epoch(old["checked_at"]):
                    out[rec["domain"]] = rec
        except (OSError, ValueError, TypeError):
            continue
    _COVERAGE_CACHE.update(sig=sig, map=out)
    return out


def _epoch(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
    except ValueError:
        return 0


def coverage_flags(dom):
    rec = latest_coverage().get(dom)
    if not rec:
        return {"coverage": None, "verified": False, "coverage_verdict": "未核对",
                "coverage_checked_at": None, "coverage_stale": False, "coverage_detail": "尚无核对记录"}
    cov = rec.get("coverage")
    verdict = rec.get("verdict") or ""
    checked = rec.get("checked_at")
    age = datetime.now(timezone.utc).timestamp() - _epoch(checked)
    stale = age > float(os.environ.get("COVERAGE_TTL_HOURS", "24")) * 3600
    reason = "报告已过期" if stale else ""
    for kind, got in (rec.get("got") or {}).items():
        path = os.path.join(DATA, dom, "urls.txt" if kind == "posts" else kind + ".urls.txt")
        cp = os.path.join(DATA, dom, "cursor.json" if kind == "posts" else kind + ".cursor.json")
        if count_lines(path) != got or (os.path.exists(cp) and os.path.getmtime(cp) > _epoch(checked)):
            stale, reason = True, "采集数据或状态已变化"
    if not verdict and cov is None and str(rec.get("source") or "").startswith("unverifiable"):
        verdict = "不可验证"
    if verdict == "口径异常" or (isinstance(cov, (int, float)) and cov >= COVERAGE_ANOMALY):
        verdict, cov = "口径异常", None
    elif verdict in ("不可验证", "核对失败"):
        cov = None
    elif verdict == "有缺口" or rec.get("crawl_unresolved"):
        verdict = "覆盖率不足"  # even >=95% cannot erase explicit unresolved work
    elif isinstance(cov, (int, float)):
        verdict = "已核对" if cov >= 0.95 else "覆盖率不足"
    else:
        verdict = verdict or "未核对"
    return {"coverage": cov, "verified": verdict == "已核对" and not stale,
            "coverage_verdict": verdict, "coverage_checked_at": checked,
            "coverage_stale": stale, "coverage_detail": reason or rec.get("verdict_detail") or rec.get("source") or "",
            "coverage_missing": (rec.get("missing") or {}).get("count"), "coverage_per_kind": rec.get("per_kind") or {}}


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


def site_row(d, cfg=None):
    dom = os.path.basename(d)
    if cfg is None:
        cfg = source_cfg().get(dom, {})
    os.makedirs(d, exist_ok=True)
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
        f = os.path.join(d, f"{kind}.urls.txt")
        c = os.path.join(d, f"{kind}.cursor.json")
        n = count_lines(f) if os.path.exists(f) else 0
        cur2 = {}
        if os.path.exists(c):
            try: cur2 = json.load(open(c))
            except Exception: pass
        return {"n": n, "page": cur2.get("next_page", 0), "pages": cur2.get("total_pages"),
                "done": cur2.get("done", False), "status": cur2.get("status", ""), "last_reason": cur2.get("last_reason", ""), "updated": (cur2.get("updated") or "")[:19]}
    collections = [kind for kind in site_kinds(cfg) if re.fullmatch(r"[a-z][a-z0-9_-]*", kind)] if cfg.get("base") and cfg.get("channel") else []
    kinds = {kind: kind_count(kind) for kind in dict.fromkeys(("pages", "media", *collections)) if kind != "posts"}
    kinds["posts"] = {"n": n, "page": cur.get("next_page", 0), "pages": cur.get("total_pages"),
                      "done": cur.get("done", False), "status": cur.get("status", ""),
                      "last_reason": cur.get("last_reason", ""), "updated": (cur.get("updated") or "")[:19]}
    return {"domain": dom, "saved": n,
            "page": cur.get("next_page", 0), "pages": cur.get("total_pages"),
            "posts": cur.get("total_posts"), "done": cur.get("done", False),
            "updated": (cur.get("updated") or "")[:19], "mb": round(chars / 1e6, 2),
            "pages_n": kinds["pages"]["n"], "media_n": kinds["media"]["n"],
            "channel": cfg.get("channel", ""), "collections": collections,
            "can_retry_links": cfg.get("channel") in REQUEST_QUEUE_CHANNELS,
            # 三类必须分开：任务结束（done，前端另有"不完整"判定）、覆盖率已核对（verified）、
            # 缺入口或不可访问（needs_config / disabled）。disabled 站不再算"待配置"。
            **cov,
            "needs_config": bool(not disabled and cfg.get("channel") == "html"
                                 and not (cfg.get("list_url") or cfg.get("list_urls"))),
            "disabled": disabled, "disabled_reason": cfg.get("disabled_reason", ""),
            "kinds": kinds}

_last = {"total": None, "at": 0.0, "rate": 0.0}

def source_cfg():
    try:
        import yaml
        return {x["domain"]: x for x in yaml.safe_load(open(sources_path(ROOT)))}
    except Exception:
        return {}

def all_domains():
    """Every configured site, not just the ones that already produced rows."""
    try:
        import yaml
        sites = yaml.safe_load(open(sources_path(ROOT)))
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

def build_status(web_domains=None):
    import time as _t
    global _last
    sites = source_cfg()
    rows = [site_row(os.path.join(DATA, d), sites.get(d, {})) for d in all_domains()]
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
            "running": running() if web_domains is None else bool(web_domains),
            "queue_running": queue_running() if web_domains is None else False,
            "tasks_running": tasks_running() if web_domains is None else len(web_domains),
            "running_domains": running_domains() if web_domains is None else sorted(web_domains), "blocked": blocked_domains(),
            "concurrency": concurrency(), "total": total, "sites": rows}

def main():
    state = build_status()
    json.dump(state, open(os.path.join(DATA, "status.json"), "w"), ensure_ascii=False)

if __name__ == "__main__":
    main()
