#!/usr/bin/env python3
"""Concurrent gap backfill: runs collect.py for a list of (site, kind) tasks."""
import os, subprocess, sys, time, json
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
PY_BIN = sys.executable
ENV = dict(os.environ, SITE_WORKERS="4")

def rows(domain, kind="posts"):
    p = os.path.join(DATA, domain, "urls.txt" if kind == "posts" else f"{kind}.urls.txt")
    return sum(1 for _ in open(p)) if os.path.exists(p) else 0

def run(task):
    domain, kind = task
    cmd = [PY_BIN, os.path.join(ROOT, "collect.py"), "--site", domain, "--limit", "0"]
    if kind != "posts":
        cmd += ["--kind", kind]
    before = rows(domain, kind)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, env=ENV)
        out = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
    except Exception as e:
        out = f"error {e}"
    after = rows(domain, kind)
    return {"task": task, "before": before, "after": after, "delta": after - before, "out": out[:160]}

def main():
    tasks = json.load(open(os.path.join(os.path.dirname(ROOT), "backfill_tasks.json")))
    log = open(os.path.join(DATA, "backfill.log"), "a")
    log.write(f"\n=== backfill {len(tasks)} tasks @ {time.strftime('%H:%M:%S')} ===\n"); log.flush()
    with ThreadPoolExecutor(max_workers=int(os.environ.get("BACKFILL_WORKERS", "4"))) as ex:
        futs = {ex.submit(run, (t["domain"], t.get("kind", "posts"))): t for t in tasks}
        for f in as_completed(futs):
            r = f.result()
            line = f"{r['task'][0]:<30}{r['task'][1]:<6}{r['before']:>8} -> {r['after']:<8} ({r['delta']:+d})  {r['out']}"
            print(line, flush=True); log.write(line + "\n"); log.flush()
    log.write("=== backfill done ===\n"); log.close()

if __name__ == "__main__":
    main()
