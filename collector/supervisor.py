#!/usr/bin/env python3
"""采集守护进程：按优先级补采剩余缺口（移植自 演练场（ignore)/supervise.py）。

- 每个任务 = 一个 (domain, kind)，调用 collector/collect.py 断点续跑
- 同站串行、最多 N 个站并发（默认 4）
- 结合进程退出码、游标和剩余工作判定完成、空闲或重试
- 暂停开关：存在 data/CRAWL_PAUSED 就停掉所有子进程，删除后自动恢复
- 按站跳过：data/CRAWL_SKIP 里 `domain:kind` 的任务保持暂停
- 看门狗：采集日志、游标和写入均无进展时终止进程组
- 空闲常驻：队列空也不退出
- 状态每 30s 原子写 data/supervise_state.json，并写 data/health.json

启动：
  "演练场（ignore)/.venv/bin/python" collector/supervisor.py
  collectorctl run
  CRAWL_RESUME_STATE=1 python collector/supervisor.py  # 延续已有队列和重试次数
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from contextlib import closing

from crawler import config
from crawler.processes import signal_group, stop_group
from crawler.request import RequestQueue

HERE = Path(__file__).resolve().parent          # collector/
PROJECT = HERE.parent
DATA = Path(config.DATA)
COLLECT = HERE / "collect.py"
PY = os.environ.get("CRAWL_PY") or sys.executable
LOG = DATA / "supervise.log"
STATE = DATA / "supervise_state.json"
HEALTH = DATA / "health.json"
PAUSE = DATA / "CRAWL_PAUSED"
SKIP = DATA / "CRAWL_SKIP"

MAX_CONCURRENT = int(os.environ.get("CRAWL_CONCURRENCY", "4"))
MAX_ATTEMPTS = int(os.environ.get("CRAWL_ATTEMPTS", "3"))
SITE_WORKERS = os.environ.get("SITE_WORKERS", "4")
RETRY_DELAY = int(os.environ.get("CRAWL_RETRY_DELAY", "90"))
STALL_KILL = int(os.environ.get("CRAWL_STALL_KILL", "1200"))          # 20min 零增长
QUEUE_RETRY_COOLDOWN = int(os.environ.get("RETRY_COOLDOWN", "900"))
STATE_EVERY = int(os.environ.get("CRAWL_STATE_EVERY", "30"))

SOURCES = config.catalog_path(HERE)


def load_tasks(sources_path=None) -> list:
    """Queue collections for enabled sites in the operator's catalog."""
    import yaml
    from crawler.config import site_kinds

    path = Path(sources_path) if sources_path is not None else SOURCES
    if not path.exists():
        return []
    sites = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    force = config.bypass()          # data/CRAWL_BYPASS：disabled 站也入队
    out = []
    for s in sites:
        if not isinstance(s, dict) or not s.get("domain"):
            continue
        if s.get("disabled") and not force:
            continue
        dom = s["domain"]
        out.extend((dom, kind) for kind in site_kinds(s))
    return out


# ---------------------------------------------------------------- 纯函数/状态
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def read_skip(path=None) -> set[str]:
    """解析 data/CRAWL_SKIP（`domain:kind` 或 `# 注释`；支持行尾注释）。"""
    p = Path(path) if path else SKIP
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    out = set()
    for ln in lines:
        ln = ln.split("#", 1)[0].strip()      # 行尾注释：www.x:posts  # 原因
        if ln:
            out.add(ln)
    return out


def _count(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def rows(data_dir, dom: str, kind: str = "posts") -> int:
    d = Path(data_dir) / dom
    primary = d / ("urls.txt" if kind == "posts" else f"{kind}.urls.txt")
    if primary.exists():
        return _count(primary)
    return _count(d / ("articles.jsonl" if kind == "posts" else f"{kind}.jsonl"))


def is_done(data_dir, dom: str, kind: str = "posts") -> bool:
    try:
        cur = read_cursor(data_dir, dom, kind)
        revision = (site_settings(dom).get("backfill_revision") or {}).get(kind, 0)
        return bool(cur.get("done")) and cur.get("backfill_revision", 0) >= revision
    except (ValueError, TypeError, AttributeError):
        return False


def site_settings(domain):
    import yaml
    try:
        sites = yaml.safe_load(SOURCES.read_text(encoding="utf-8")) or []
        return next((site for site in sites if isinstance(site, dict) and site.get("domain") == domain), {})
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        return {}


def classify_exit(done: bool, attempts: int, max_attempts=None, *,
                  returncode=0, no_work=False, progress=False) -> tuple[str, int]:
    """子进程退出后的处置：completed / retry / stalled，以及累计尝试次数。"""
    if returncode == 0 and done:
        return "completed", attempts
    if returncode == 0 and no_work:
        return "idle", attempts
    limit = MAX_ATTEMPTS if max_attempts is None else max_attempts
    n = 0 if progress else attempts + 1
    return ("stalled" if n >= limit else "retry"), n


def watchdog_action(now, last_progress, last_rows, cur_rows, stall_kill=None,
                    check_interval: int = 120) -> str | None:
    """看门狗判定：None=还不到检查点，progress=有增长，kill=零增长超时。"""
    limit = STALL_KILL if stall_kill is None else stall_kill
    if now - last_progress < check_interval:
        return None
    if cur_rows > last_rows:
        return "progress"
    if now - last_progress >= limit:
        return "kill"
    return "wait"


def state_payload(data_dir, running: dict, queue, completed, stalled, attempts,
                  paused_by_user=None, now=None, idle=()) -> dict:
    """组装 data/supervise_state.json 的内容。"""
    now = time.time() if now is None else now
    return {
        "updated": now_iso(),
        "running": [{
            "domain": d, "kind": k,
            "elapsed_sec": round(now - info["start"]),
            "rows": rows(data_dir, d, k),
            "delta": rows(data_dir, d, k) - info["before"],
            "log": f"data/crawl-{d}-{k}.log",
        } for (d, k), info in running.items()],
        "pending": [f"{d}:{k}" for d, k in queue],
        "completed": [f"{d}:{k}" for d, k in completed],
        "stalled": [f"{d}:{k}" for d, k in stalled],
        "idle": [f"{d}:{k}" for d, k in idle],
        "paused_by_user": sorted(paused_by_user or ()),
        "attempts": {f"{d}:{k}": n for (d, k), n in attempts.items()},
    }


def write_state(payload_or_path=None, path=None, **payload) -> dict:
    """原子写状态文件。支持 write_state(payload, path=...) 或 write_state(path=..., **kw)。"""
    if isinstance(payload_or_path, (str, Path)):
        path = path or payload_or_path
        body = payload
    else:
        body = payload_or_path if payload_or_path is not None else payload
    target = Path(path) if path else STATE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    os.replace(tmp, target)
    return body


def log(msg: str) -> None:
    line = f"{now_iso()} {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _write_health(data_dir, running: dict, queue, stalled, completed) -> None:
    """每轮写 data/health.json；失败不影响采集。"""
    try:
        import health
        health.write_health(
            Path(data_dir) / "health.json",
            data_dir=data_dir,
            running=[f"{d}:{k}" for (d, k) in running],
            pending=len(queue),
            stalled=len(stalled),
            done=len(completed),
            paused=(Path(data_dir) / "CRAWL_PAUSED").exists(),
        )
    except Exception as exc:                     # health 缺依赖/坏数据时不能拖垮守护
        log(f"health write failed: {type(exc).__name__}: {exc}")


def start(key, running, data_dir=None, py=None, env=None) -> None:
    dom, kind = key
    data_dir = Path(data_dir) if data_dir else DATA
    cmd = [str(py or PY), str(COLLECT), "--site", dom, "--kind", kind, "--limit", "0", "--no-csv"]
    run_env = dict(os.environ if env is None else env, SITE_WORKERS=SITE_WORKERS,
                   CRAWL_DATA_DIR=str(data_dir.resolve()), PYTHONUNBUFFERED="1")
    run_env.setdefault("QUEUE_BATCH", "100")
    logfile = data_dir / f"crawl-{dom}-{kind}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    fh = open(logfile, "a", encoding="utf-8")
    fh.write(f"\n===== {now_iso()} start {dom} {kind} =====\n")
    fh.flush()
    before = rows(data_dir, dom, kind)
    progress = progress_signature(data_dir, key, logfile)
    initial_cursor = read_cursor(data_dir, dom, kind)
    try:
        proc = subprocess.Popen(cmd, cwd=str(PROJECT), env=run_env, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)
    except BaseException:
        fh.close()
        raise
    running[key] = {"proc": proc, "fh": fh, "start": time.time(), "before": before,
                    "logfile": logfile, "progress": progress, "initial_cursor": initial_cursor,
                    "last_progress": time.monotonic()}
    log(f"start {dom} {kind} (running={len(running)}/{MAX_CONCURRENT}) rows={before}")


def read_cursor(data_dir, dom, kind):
    name = "cursor.json" if kind == "posts" else f"{kind}.cursor.json"
    try:
        cur = json.loads((Path(data_dir) / dom / name).read_text(encoding="utf-8"))
        return cur if isinstance(cur, dict) else {}
    except (OSError, ValueError):
        return {}


def progress_signature(data_dir, key, logfile):
    dom, kind = key
    directory = Path(data_dir) / dom
    prefix = "" if kind == "posts" else f"{kind}."
    paths = (logfile, directory / f"{prefix}cursor.json", directory / f"{prefix}urls.txt")
    result = []
    for path in paths:
        try:
            stat = path.stat()
            result.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            result.append(None)
    return tuple(result)


def request_work(data_dir, key, *, include_skipped=False):
    """Return pending work and whether dead letters can be retried."""
    path = Path(data_dir) / "state.db"
    if not path.is_file():
        return False, False
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            states = ("unprocessed", "inflight", "skipped" if include_skipped else "unprocessed")
            pending = conn.execute(
                "SELECT 1 FROM requests WHERE domain=? AND kind=? AND state IN (?,?,?) LIMIT 1",
                (*key, *states)).fetchone()
            if pending:
                return True, False
            errors = conn.execute("SELECT error FROM requests WHERE domain=? AND kind=? AND state='error'", key)
            statuses = site_settings(key[0]).get("retry_http_statuses") or ()
            return False, any(RequestQueue.retryable_error(error, statuses) for (error,) in errors)
    except sqlite3.OperationalError as exc:
        return ("no such table" not in str(exc)), False


def resume_snapshot(tasks, path):
    state = json.loads(path.read_text(encoding="utf-8"))
    by_name = {f"{dom}:{kind}": (dom, kind) for dom, kind in tasks}

    def known(names):
        return list(dict.fromkeys(by_name[name] for name in names if name in by_name))

    completed, stalled, idle, paused = (
        known(state.get(field, ())) for field in ("completed", "stalled", "idle", "paused_by_user")
    )
    parked = set(completed + stalled + idle + paused)
    running = [f"{job['domain']}:{job['kind']}" for job in state.get("running", ())]
    queue = [key for key in known(running + state["pending"]) if key not in parked]
    attempts = {by_name[name]: value for name, value in state.get("attempts", {}).items()
                if name in by_name}
    return queue, completed, stalled, idle, set(paused), attempts


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    queue = list(dict.fromkeys(load_tasks()))
    running = {}
    completed, stalled, idle = [], [], []
    paused = set()
    attempts, ready_at = {}, {}
    registrations = set()
    if os.environ.get("CRAWL_RESUME_STATE") == "1" and STATE.is_file():
        queue, completed, stalled, idle, paused, attempts = resume_snapshot(queue, STATE)
        registrations.update(json.loads(STATE.read_text(encoding="utf-8")).get("registrations", ()))
        log(f"resume snapshot pending={len(queue)} completed={len(completed)} "
            f"idle={len(idle)} stalled={len(stalled)} paused={len(paused)}")
    stop = False
    last_state = 0.0
    prior_pause = False

    def publish():
        payload = state_payload(DATA, running, queue, completed, stalled, attempts,
                                paused_by_user=[f"{d}:{k}" for d, k in paused], idle=idle)
        if registrations:
            payload["registrations"] = sorted(registrations)
        write_state(payload)
        _write_health(DATA, running, queue, stalled, completed)

    def accept_registrations(skip):
        requests = sorted((DATA / "supervisor-inbox").glob("*.json"))
        if not requests:
            return
        import yaml
        try:
            configured = set(load_tasks())
        except (OSError, ValueError, yaml.YAMLError):
            return
        acknowledged = []
        for path in requests:
            if path.stem not in registrations:
                try:
                    request = json.loads(path.read_text(encoding="utf-8"))
                    domain, kinds = request["domain"], request["kinds"]
                    if not isinstance(domain, str) or not isinstance(kinds, list) or not all(isinstance(kind, str) for kind in kinds):
                        continue
                    keys = [(domain, kind) for kind in kinds]
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if not keys or not all(key in configured for key in keys):
                    continue
                for key in keys:
                    if key in running or key in queue or key in paused:
                        continue
                    for parked in (completed, stalled, idle):
                        if key in parked:
                            parked.remove(key)
                    attempts.pop(key, None)
                    ready_at.pop(key, None)
                    if f"{key[0]}:{key[1]}" in skip:
                        paused.add(key)
                    else:
                        queue.append(key)
                registrations.add(path.stem)
                log(f"registered {domain} collections={','.join(kinds)} job={path.stem}")
            acknowledged.append(path)
        if acknowledged:
            # Persist admission before deleting the request or starting a child.
            publish()
            for path in acknowledged:
                path.unlink(missing_ok=True)

    def on_term(*_):
        nonlocal stop
        stop = True

    def settle(key, rc, info=None):
        dom, kind = key
        after = rows(DATA, dom, kind) if info else 0
        delta = after - info["before"] if info else 0
        cur = read_cursor(DATA, dom, kind)
        reason = cur.get("last_reason")
        pending, transient = request_work(DATA, key) if info else (False, False)
        fresh = bool(info and cur != info["initial_cursor"])
        next_page = cur.get("next_page")
        prior_page = info["initial_cursor"].get("next_page") if info else None
        more_pages = fresh and isinstance(next_page, int) and next_page > (prior_page if isinstance(prior_page, int) else 1)
        no_work = (fresh and not pending and not transient and
                   reason in ("nothing_new", "no_output", "dead_404", "dead_letters", "skipped"))
        action, n = classify_exit(
            bool(info and is_done(DATA, dom, kind)), attempts.get(key, 0),
            returncode=rc, no_work=no_work, progress=delta > 0 or more_pages,
        )
        log(f"exit {dom} {kind} rc={rc} delta={delta:+d} rows={after} "
            f"status={cur.get('status')} reason={reason} -> {action}")
        attempts[key] = n
        if action == "completed":
            completed.append(key)
        elif action == "idle":
            idle.append(key)
        elif action == "stalled":
            stalled.append(key)
            log(f"STALLED {dom} {kind} after {n} consecutive attempts")
        else:
            delay = max(RETRY_DELAY, QUEUE_RETRY_COOLDOWN if transient else 0)
            ready_at[key] = time.monotonic() + delay
            queue.append(key)
            log(f"retry {dom} {kind} in {delay}s (attempt {n}/{MAX_ATTEMPTS})")

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    while not stop:
        skip = read_skip()
        globally_paused = PAUSE.exists()
        if globally_paused != prior_pause:
            log("PAUSED by CRAWL_PAUSED" if globally_paused else "RESUMED after CRAWL_PAUSED")
            prior_pause = globally_paused

        for key, info in list(running.items()):
            proc = info["proc"]
            site_paused = f"{key[0]}:{key[1]}" in skip
            if globally_paused or site_paused:
                if stop_group(proc) is None:
                    continue
                info["fh"].close()
                running.pop(key)
                if site_paused:
                    paused.add(key)
                else:
                    queue.insert(0, key)
                continue

            rc = proc.poll()
            if rc is not None:
                signal_group(proc.pid, signal.SIGKILL)
                info["fh"].close()
                running.pop(key)
                settle(key, rc, info)
                continue

            now = time.monotonic()
            signature = progress_signature(DATA, key, info["logfile"])
            action = watchdog_action(now, info["last_progress"], 0, int(signature != info["progress"]),
                                     STALL_KILL, check_interval=0)
            if action == "progress":
                info["progress"] = signature
                info["last_progress"] = now
            elif action == "kill":
                log(f"watchdog: stop {key[0]} {key[1]} (no task progress for {STALL_KILL}s)")
                stop_group(proc)

        for parked in (queue, idle, stalled):
            for key in list(parked):
                if f"{key[0]}:{key[1]}" in skip:
                    parked.remove(key)
                    paused.add(key)
        for key in sorted(paused):
            if f"{key[0]}:{key[1]}" not in skip:
                paused.remove(key)
                attempts.pop(key, None)
                ready_at.pop(key, None)
                queue.append(key)
                log(f"resume {key[0]} {key[1]} after CRAWL_SKIP")

        accept_registrations(skip)

        if not globally_paused:
            for key in list(queue):
                if len(running) >= MAX_CONCURRENT:
                    break
                if ready_at.get(key, 0) > time.monotonic() or any(k[0] == key[0] for k in running):
                    continue
                queue.remove(key)
                if is_done(DATA, *key) and not any(request_work(DATA, key, include_skipped=True)):
                    completed.append(key)
                    log(f"skip {key[0]} {key[1]} (cursor done)")
                    continue
                try:
                    start(key, running)
                except OSError as exc:
                    log(f"start failed {key[0]} {key[1]}: {exc}")
                    settle(key, 1)

        now = time.monotonic()
        if now - last_state >= STATE_EVERY:
            publish()
            last_state = now
        time.sleep(5)

    for key, info in list(running.items()):
        if stop_group(info["proc"]) is not None:
            info["fh"].close()
            running.pop(key)
            queue.insert(0, key)
    publish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
