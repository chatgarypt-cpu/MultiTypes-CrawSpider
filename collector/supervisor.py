#!/usr/bin/env python3
"""采集守护进程：按优先级补采剩余缺口（移植自 演练场（ignore)/supervise.py）。

- 每个任务 = 一个 (domain, kind)，调用 collector/collect.py 断点续跑
- 同站串行、最多 N 个站并发（默认 4）
- 进程退出后看 cursor.done 判定完成；未完成则退避重试，最多 3 次
- 暂停开关：存在 data/CRAWL_PAUSED 就停掉所有子进程，删除后自动恢复
- 按站跳过：data/CRAWL_SKIP 里 `domain:kind` 的任务保持暂停
- 看门狗：行数 20 分钟零增长杀进程；stalled 任务 30 分钟重排一次
- 空闲常驻：队列空也不退出
- 状态每 30s 原子写 data/supervise_state.json，并写 data/health.json

启动：
  "演练场（ignore)/.venv/bin/python" collector/supervisor.py
  collectorctl run
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # collector/
PROJECT = HERE.parent
DATA = HERE / "data"                            # collector/data -> ../data
COLLECT = HERE / "collect.py"
PY = os.environ.get("CRAWL_PY") or sys.executable
LOG = DATA / "supervise.log"
STATE = DATA / "supervise_state.json"
HEALTH = DATA / "health.json"
PAUSE = DATA / "CRAWL_PAUSED"
SKIP = DATA / "CRAWL_SKIP"

PAUSED_BY_USER: set[str] = set()

MAX_CONCURRENT = int(os.environ.get("CRAWL_CONCURRENCY", "4"))
MAX_ATTEMPTS = int(os.environ.get("CRAWL_ATTEMPTS", "3"))
SITE_WORKERS = os.environ.get("SITE_WORKERS", "4")
RETRY_DELAY = int(os.environ.get("CRAWL_RETRY_DELAY", "90"))
STALL_KILL = int(os.environ.get("CRAWL_STALL_KILL", "1200"))          # 20min 零增长
STALLED_RETRY = int(os.environ.get("CRAWL_STALLED_RETRY", "1800"))    # stalled 30min 重排
STATE_EVERY = int(os.environ.get("CRAWL_STATE_EVERY", "30"))

# 优先级：先最大缺口，再按缺量从大到小（与旧 supervise.py 一致）
TASKS = [
    ("www.spc.int", "posts"),
    # 2026-11-09 补采：spc 只采了 1/3（2,592/约7,700 blog）；其余是核对发现的缺口/新入口
    ("fijisun.com.fj", "posts"),
    ("www.cookislandsnews.com", "pages"),
    ("www.nus.edu.ws", "pages"),
    ("www.palaugov.pw", "pages"),
    ("www.mvariety.com", "posts"),
    ("www.samoaobserver.ws", "posts"),
    ("www.gov.nu", "posts"),
    ("www.upng.ac.pg", "posts"),
    ("www.coralgardeners.org", "posts"),
    ("www.mecdm.gov.sb", "posts"),
    ("www.cookislandsnews.com", "posts"),
    ("matangitonga.to", "posts"),
    ("www.wpcouncil.org", "posts"),
    ("www.maisondelaculture.pf", "posts"),
    ("radiosamoa.ws", "posts"),
    ("www.redcross.com.fj", "posts"),
    ("www.palaugov.pw", "posts"),
    ("www.gov.to", "posts"),
    ("www.sinu.edu.sb", "posts"),
    ("www.comfsm.fm", "posts"),
    ("www.ilm.pf", "posts"),
    ("www.palau.edu", "posts"),
    ("www.nus.edu.ws", "posts"),
    ("www.norfolkisland.gov.nf", "posts"),
    ("www.fwrm.org.fj", "posts"),
    ("www.pngnri.org", "posts"),
    ("dimenc.gouv.nc", "posts"),
    ("la1ere.francetvinfo.fr", "posts"),
    ("www.wallis-et-futuna.gouv.fr", "posts"),
    ("www.saipantribune.com", "media"),
    ("www.lnc.nc", "posts"),
    ("www.postcourier.com.pg", "media"),
    ("www.solomonstarnews.com", "posts"),
    ("www.tntv.pf", "posts"),
    ("www.fnu.ac.fj", "media"),
    ("radiosamoa.ws", "media"),
    ("www.thenational.com.pg", "posts"),
    ("www.mnre.gov.ws", "media"),
    ("www.mnre.gov.ws", "posts"),
    ("www.mnre.gov.ws", "pages"),
    ("www.maisondelaculture.pf", "media"),
    ("www.maisondelaculture.pf", "pages"),
    ("www.wpcouncil.org", "media"),
    ("www.wpcouncil.org", "pages"),
    ("emtv.com.pg", "posts"),
    ("www.tntv.pf", "media"),
    ("www.thenational.com.pg", "media"),
    ("pina.com.fj", "pages"),
    ("pina.com.fj", "media"),
    ("www.sinu.edu.sb", "media"),
    ("www.palaugov.pw", "media"),
    ("www.palaugov.pw", "posts"),
    ("www.vbtc.vu", "media"),
    ("dcrm.gov.mp", "media"),
    ("www.redcross.com.fj", "posts"),
    ("www.gov.to", "posts"),
    ("www.gov.to", "media"),
    ("www.nus.edu.ws", "media"),
    ("www.comfsm.fm", "media"),
    ("www.criobe.pf", "media"),
    ("www.palau.edu", "posts"),
    ("www.transparencypng.org.pg", "media"),
    ("www.pngimr.org.pg", "pages"),
    ("www.pngimr.org.pg", "media"),
    ("www.sinu.edu.sb", "posts"),
    ("www.vbtc.vu", "posts"),
    ("www.maisondelaculture.pf", "posts"),
    ("www.postcourier.com.pg", "posts"),
    ("pina.com.fj", "posts"),
    # C 组（2026-11-09 补入口后加入；站小，放队尾）
    ("www.upf.pf", "posts"),
    ("www.marianas.edu", "posts"),
    ("www.palauconservation.org", "posts"),
    ("www.adck.nc", "posts"),
    ("gov.fm", "posts"),
    # 大站/慢站放最后：fbcnews 7.2 万条；dailypost 需等 429 冷却（CRAWL_SKIP 控制）
    ("www.fbcnews.com.fj", "posts"),
    ("dailypost.vu", "posts"),
]


# ---------------------------------------------------------------- 纯函数/状态
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def read_skip(path=None) -> set[str]:
    """解析 data/CRAWL_SKIP（`domain:kind` 或 `# 注释`）。"""
    p = Path(path) if path else SKIP
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}


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
    name = "cursor.json" if kind == "posts" else f"{kind}.cursor.json"
    try:
        with open(Path(data_dir) / dom / name, encoding="utf-8") as f:
            return bool(json.load(f).get("done"))
    except (OSError, ValueError, AttributeError):
        return False


def classify_exit(done: bool, attempts: int, max_attempts=None) -> tuple[str, int]:
    """子进程退出后的处置：completed / retry / stalled，以及累计尝试次数。"""
    if done:
        return "completed", attempts
    limit = MAX_ATTEMPTS if max_attempts is None else max_attempts
    n = attempts + 1
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
                  paused_by_user=None, now=None) -> dict:
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
        "paused_by_user": sorted(paused_by_user or PAUSED_BY_USER),
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
    cmd = [str(py or PY), str(COLLECT), "--site", dom, "--kind", kind, "--limit", "0"]
    run_env = dict(env or os.environ, SITE_WORKERS=SITE_WORKERS,
                   QUEUE_BATCH=os.environ.get("QUEUE_BATCH", "100"))
    logfile = data_dir / f"crawl-{dom}-{kind}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    fh = open(logfile, "a", encoding="utf-8")
    fh.write(f"\n===== {now_iso()} start {dom} {kind} =====\n")
    fh.flush()
    proc = subprocess.Popen(cmd, cwd=str(PROJECT), env=run_env, stdout=fh,
                            stderr=subprocess.STDOUT, start_new_session=True)
    before = rows(data_dir, dom, kind)
    running[key] = {"proc": proc, "fh": fh, "start": time.time(), "before": before,
                    "last_rows": before, "last_progress": time.time()}
    log(f"start {dom} {kind} (running={len(running)}/{MAX_CONCURRENT}) rows={before}")


# ---------------------------------------------------------------- 主循环
def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    queue = list(dict.fromkeys(TASKS))
    running: dict = {}
    completed: list = []
    stalled: list = []
    attempts: dict = {}
    ready_at: dict = {}
    last_state = 0.0
    last_health = 0.0
    paused_logged = False
    last_retry = 0.0

    def flush_state() -> None:
        nonlocal last_state
        write_state(state_payload(DATA, running, queue, completed, stalled, attempts,
                                  paused_by_user=PAUSED_BY_USER))
        last_state = time.time()

    def flush_health() -> None:
        nonlocal last_health
        _write_health(DATA, running, queue, stalled, completed)
        last_health = time.time()

    def on_term(*_):
        for info in running.values():
            try:
                info["proc"].terminate()
            except Exception:
                pass
        try:
            flush_state()
            flush_health()
        except Exception:
            pass
        log("supervisor terminated")
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    log(f"supervisor start: {len(queue)} tasks, concurrency={MAX_CONCURRENT}, "
        f"attempts={MAX_ATTEMPTS}, py={PY}")
    flush_state()
    flush_health()

    while True:                                  # 常驻服务：空闲也不退出
        # 0) 暂停开关：存在 data/CRAWL_PAUSED 就停掉所有子进程，删除后自动恢复
        if PAUSE.exists():
            for key, info in list(running.items()):
                try:
                    info["proc"].terminate()
                except Exception:
                    pass
                try:
                    info["fh"].close()
                except Exception:
                    pass
                running.pop(key)
                queue.insert(0, key)
            if not paused_logged:
                log("PAUSED by data/CRAWL_PAUSED — 已停止采集，删除该文件后自动恢复")
                paused_logged = True
            flush_state()
            if time.time() - last_health >= STATE_EVERY:
                flush_health()
            time.sleep(5)
            continue
        elif paused_logged:
            log("RESUMED — data/CRAWL_PAUSED 已移除")
            paused_logged = False

        # 1) 回收完成的子进程
        for key, info in list(running.items()):
            rc = info["proc"].poll()
            if rc is None:
                continue
            try:
                info["fh"].close()
            except Exception:
                pass
            running.pop(key)
            dom, kind = key
            after = rows(DATA, dom, kind)
            delta = after - info["before"]
            done = is_done(DATA, dom, kind)
            action, n = classify_exit(done, attempts.get(key, 0))
            log(f"exit {dom} {kind} rc={rc} delta={delta:+d} rows={after} "
                f"done={done} took={round(time.time() - info['start'])}s -> {action}")
            if action == "completed":
                completed.append(key)
            elif action == "stalled":
                attempts[key] = n
                stalled.append(key)
                log(f"STALLED {dom} {kind} after {n} attempts")
            else:                                 # retry
                attempts[key] = n
                ready_at[key] = time.time() + RETRY_DELAY
                queue.append(key)
                log(f"retry {dom} {kind} in {RETRY_DELAY}s (attempt {n}/{MAX_ATTEMPTS})")

        # 1.5) 看门狗：行数长时间不涨 = 连接/SSL 卡死，杀掉让重试分支接管
        now = time.time()
        for key, info in list(running.items()):
            if now - info["last_progress"] < 120:      # 每 2 分钟检查一次，避免频繁扫大文件
                continue
            dom, kind = key
            cur = rows(DATA, dom, kind)
            action = watchdog_action(now, info["last_progress"], info["last_rows"],
                                     cur, STALL_KILL)
            if action == "progress":
                info["last_rows"] = cur
                info["last_progress"] = now
            elif action == "kill":
                log(f"watchdog: kill {dom} {kind} (no new rows for {STALL_KILL}s)")
                try:
                    info["proc"].kill()
                except Exception:
                    pass
                info["last_progress"] = now

        # 2) 启动新任务（同站串行；data/CRAWL_SKIP 里列出的任务保持暂停）
        skip = read_skip(SKIP)
        for key in list(queue):
            if len(running) >= MAX_CONCURRENT:
                break
            if ready_at.get(key, 0) > time.time():
                continue
            dom, kind = key
            if any(rk[0] == dom for rk in running):
                continue
            queue.remove(key)
            if f"{dom}:{kind}" in skip:
                PAUSED_BY_USER.add(f"{dom}:{kind}")
                log(f"skip {dom} {kind} (user paused via data/CRAWL_SKIP)")
                continue
            if is_done(DATA, dom, kind):
                completed.append(key)
                log(f"skip {dom} {kind} (cursor done)")
                continue
            start(key, running)

        # 3) 落盘进度 + 健康快照
        if time.time() - last_state >= STATE_EVERY:
            flush_state()
            flush_health()

        # 4) 空闲：队列空且无子进程时保持存活，定期把 stalled 任务重新排队
        if not queue and not running:
            if time.time() - last_retry >= STALLED_RETRY:
                if stalled:
                    log("retry stalled tasks: "
                        + ", ".join(f"{d}:{k}" for d, k in stalled))
                    for key in list(stalled):
                        attempts.pop(key, None)
                        queue.append(key)
                    stalled.clear()
                last_retry = time.time()
            flush_state()
            if time.time() - last_health >= STATE_EVERY:
                flush_health()
            time.sleep(30)
            continue

        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
