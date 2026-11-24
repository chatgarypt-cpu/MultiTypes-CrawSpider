#!/usr/bin/env python3
"""Task queue: posts -> pages -> media for WordPress, posts only for the rest.

Concurrency is read from data/queue_config.json every loop (dashboard writes it).
"""
import json, os, subprocess, sys, time
import yaml

from crawler.config import sources_path

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CFG = os.path.join(DATA, "queue_config.json")
LOG = os.path.join(DATA, "queue.log")
BLOCKED = os.path.join(DATA, "queue_blocked.json")
DONE = os.path.join(DATA, "queue_done.json")

def read_cfg():
    try: return max(1, min(10, int(json.load(open(CFG)).get("concurrency", 4))))
    except Exception: return 4

def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")

def paths(domain, kind):
    d = os.path.join(DATA, domain)
    if kind == "posts":
        return (os.path.join(d, "articles.jsonl"), os.path.join(d, "cursor.json"))
    return (os.path.join(d, f"{kind}.jsonl"), os.path.join(d, f"{kind}.cursor.json"))

def finished(domain, kind):
    _, cp = paths(domain, kind)
    if not os.path.exists(cp): return False
    try: c = json.load(open(cp))
    except Exception: return False
    return bool(c.get("done")) or bool(c.get("total_pages") and c.get("next_page", 0) > c["total_pages"])

def live(domain):
    r = subprocess.run(["pgrep", "-f", f"collect.py --site {domain}"], capture_output=True, text=True)
    return bool(r.stdout.strip())

def load_done():
    try: return set(json.load(open(DONE)))
    except Exception: return set()

def save_done(s):
    json.dump(sorted(s), open(DONE, "w"))

def load_blocked():
    try: return set(json.load(open(BLOCKED)))
    except Exception: return set()

def save_blocked(s):
    json.dump(sorted(s), open(BLOCKED, "w"))

def build_tasks():
    tasks, seen = [], set()
    for s in yaml.safe_load(open(sources_path(ROOT))):
        if (s["domain"], "posts") in seen:
            continue                     # duplicate row for the same site in the source list
        seen.add((s["domain"], "posts"))
        if s["channel"] == "wp-api" and s.get("via", "direct") != "browser":
            for k in ("posts", "pages", "media"):
                tasks.append((s["domain"], k, s))
        elif s["channel"] == "html" and not s.get("list_url"):
            continue                      # no list rule yet: onboarding work, not a crawl task
        else:
            tasks.append((s["domain"], "posts", s))
    return tasks

def rebuild_nonwp(domain):
    """Non-WP rows predate the enriched extractor; wipe once so they are re-fetched."""
    marker = os.path.join(DATA, domain, ".rebuilt")
    if os.path.exists(marker): return
    for name in ("articles.jsonl", "urls.txt", "cursor.json"):
        p = os.path.join(DATA, domain, name)
        if os.path.exists(p): os.remove(p)
    os.makedirs(os.path.join(DATA, domain), exist_ok=True)
    open(marker, "w").write("1")

def main():
    os.makedirs(DATA, exist_ok=True)
    tasks = build_tasks()
    say(f"queue start: {len(tasks)} tasks")
    procs, fails = {}, {}
    blocked = load_blocked()
    done = load_done()
    while True:
        n = read_cfg()
        for key, (p, fh, site) in list(procs.items()):
            if p.poll() is None: continue
            fh.close(); procs.pop(key)
            dom, kind = key
            ok = p.returncode == 0
            # a non-WordPress run that produced no rows is a failure, not a success
            if ok and site["channel"] != "wp-api" and kind == "posts":
                f = os.path.join(DATA, dom, "articles.jsonl")
                ok = os.path.exists(f) and os.path.getsize(f) > 0
            if ok:
                fails.pop(key, None); done.add(f"{dom}:{kind}"); save_done(done); say(f"done {dom} {kind}")
            else:
                fails[key] = fails.get(key, 0) + 1
                if fails[key] >= 2:
                    blocked.add(f"{dom}:{kind}"); save_blocked(blocked); say(f"BLOCKED {dom} {kind}")
                else:
                    say(f"retry {dom} {kind} (exit {p.returncode})")
        active_domains = {k[0] for k in procs}
        pending = []
        for dom, kind, site in tasks:
            if dom in active_domains: continue
            if f"{dom}:{kind}" in blocked or f"{dom}:{kind}" in done or (dom, kind) in procs: continue
            if finished(dom, kind) or live(dom): continue
            pending.append((dom, kind, site))
        for dom, kind, site in pending:
            if len(procs) >= n: break
            if dom in active_domains: continue
            if site["channel"] != "wp-api":
                rebuild_nonwp(dom)
            fh = open(os.path.join(DATA, f"run-{dom}-{kind}.log"), "a")
            cmd = [sys.executable, os.path.join(ROOT, "collect.py"), "--site", dom, "--limit", "0", "--no-csv"]
            if kind != "posts": cmd += ["--kind", kind]
            procs[(dom, kind)] = (subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT), fh, site)
            active_domains.add(dom)
            say(f"start {dom} {kind} (running={len(procs)}/{n})")
        if not pending and not procs:
            say("queue empty"); break
        time.sleep(3)

if __name__ == "__main__":
    main()
