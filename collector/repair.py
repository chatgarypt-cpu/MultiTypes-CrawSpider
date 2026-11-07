#!/usr/bin/env python3
"""Re-walk sites whose live totals exceed what is on disk.

Keeps cursors (they resume where a transient failure stopped); repeats a site/kind
until a run adds nothing. Resets only cursors that disagree with the data on disk.
"""
import json, os, subprocess, sys
import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
LOG = os.path.join(DATA, "repair.log")
ROUNDS = 12

def rows(domain, kind):
    f = os.path.join(DATA, domain, "urls.txt" if kind == "posts" else f"{kind}.urls.txt")
    return sum(1 for _ in open(f)) if os.path.exists(f) else 0

def sync_cursor(domain, kind):
    """If the cursor was clobbered by an earlier bug, rebuild it from the rows on disk."""
    cp = os.path.join(DATA, domain, "cursor.json" if kind == "posts" else f"{kind}.cursor.json")
    if not os.path.exists(cp):
        return
    try:
        c = json.load(open(cp))
    except Exception:
        os.remove(cp); return
    n = rows(domain, kind)
    if c.get("done") and c.get("seen", 0) < (c.get("total_posts") or 0):
        c["done"] = False                 # a transient 400 had ended the crawl early
    if c.get("saved", 0) == n and not c.get("done"):
        json.dump(c, open(cp, "w"))
        return
    per = 50
    c["saved"] = n
    c["next_page"] = n // per + 1
    c["total_pages"] = c.get("total_pages") or None
    c["done"] = False
    json.dump(c, open(cp, "w"))

def main():
    gaps = json.load(open(os.path.join(DATA, "audit_gaps.json")))
    sites = {s["domain"]: s for s in yaml.safe_load(open(os.path.join(ROOT, "sources.yaml")))}
    todo = []
    for g in gaps:
        if not g.get("expected"): continue
        s = sites.get(g["domain"], {})
        if s.get("via") == "browser": continue
        ratio = g["got"] / g["expected"]
        if g["kind"] == "posts" and ratio >= 0.8: continue
        todo.append((g["domain"], g["kind"], g["expected"]))
    todo.sort(key=lambda t: -(t[2] - rows(t[0], t[1])))
    log = open(LOG, "a")
    log.write(f"\n=== repair {len(todo)} tasks ===\n"); log.flush()
    for dom, kind, exp in todo:
        sync_cursor(dom, kind)
        for round_no in range(1, ROUNDS + 1):
            before = rows(dom, kind)
            cmd = [sys.executable, os.path.join(ROOT, "collect.py"), "--site", dom, "--limit", "0"]
            if kind != "posts": cmd += ["--kind", kind]
            r = subprocess.run(cmd, capture_output=True, text=True)
            after = rows(dom, kind)
            log.write(f"[{dom} {kind} r{round_no}] {before}->{after}/{exp}\n"); log.flush()
            if after <= before or after >= exp * 0.98:
                break
    log.write("=== repair done ===\n"); log.close()

if __name__ == "__main__":
    main()
