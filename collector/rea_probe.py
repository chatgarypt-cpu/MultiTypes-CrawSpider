#!/usr/bin/env python3
"""Classify html-channel sites: does the page talk to a JSON API, or is it SSR/static?

Runs one REA headless capture per site, then (only when an API shows up) the project's
offline inference (skills/web-reverse-crawl/scripts/infer_api.py) to produce a client.
"""
import concurrent.futures as cf
import json, os, re, subprocess, sys, tempfile
from urllib.parse import urlparse
import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
NODE = "/Users/gary/.local/share/easel-runtime/node-v24.21.0-darwin-arm64/bin/node"
REA = "/Users/gary/.npm/_npx/9b9809298fe40d2c/node_modules/rea-agents/scripts/rea.mjs"
INFER = os.path.join(ROOT, "..", "skills", "web-reverse-crawl", "scripts", "infer_api.py")
OUT = os.path.join(ROOT, "data", "rea")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

def scenario(url):
    return {"browser": {"mode": "launch", "executable_path": CHROME, "headless": True},
            "start_url": {"url": url, "query": []},
            "actions": [{"step_id": "settle", "action": "wait_for_timeout", "duration_ms": 4000}],
            "capture": {"events": ["network"], "at_end": ["url"],
                        "network": {"request_body": False, "response_body": False, "header_values": False}}}

def probe(site):
    d = site["domain"]; url = site["base"] + "/"
    os.makedirs(OUT, exist_ok=True)
    ev_path = os.path.join(OUT, d + ".json")
    rec = {"domain": d, "url": url}
    if os.path.exists(ev_path) and os.path.getsize(ev_path) > 1000:
        rec["cache"] = True
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(scenario(url), f); scn = f.name
        try:
            p = subprocess.run([NODE, REA, "capture-browser-scenario", scn, "--format", "json"],
                               capture_output=True, text=True, timeout=120)
            open(ev_path, "w").write(p.stdout)
        except subprocess.TimeoutExpired:
            rec["verdict"] = "timeout"; return rec
        finally:
            os.unlink(scn)
    try:
        ev = json.load(open(ev_path))
    except Exception:
        rec["verdict"] = "bad-json"; return rec
    if "error" in ev:
        rec["verdict"] = "capture-error"; rec["err"] = ev["error"][:60]; return rec
    nr = ev["normalized_result"]
    items = nr["events"]["items"]
    origin = urlparse(nr["steps"][0]["after_url"]["url"]).netloc
    same, xhr, api = 0, 0, 0
    for it in items:
        u = it.get("url") or {}
        if urlparse(u.get("url", "")).netloc != origin: continue
        same += 1
        if it.get("resource_type") in ("xhr", "fetch"):
            xhr += 1
            if re.search(r"/api/|\.json|graphql|/v\d+/|/wp-json|/rest/", u.get("url", "")): api += 1
    rec.update({"events": nr["events"]["retained"], "same": same, "xhr": xhr, "json": api,
                "final": nr["steps"][0]["after_url"]["url"]})
    rec["verdict"] = "api" if (api or xhr >= 3) else ("ssr" if same >= 5 else "blocked/empty")
    if rec["verdict"] == "api":
        out = os.path.join(OUT, d + "-api")
        subprocess.run([sys.executable, INFER, ev_path, "--out", out, "--origin", site["base"]],
                       capture_output=True, text=True, timeout=180)
        rec["client"] = os.path.exists(os.path.join(out, "api-spec", "client.mjs"))
    return rec

def main():
    sites = [s for s in yaml.safe_load(open(os.path.join(ROOT, "sources.yaml"))) if s["channel"] == "html"]
    seen, uniq = set(), []
    for s in sites:
        if s["domain"] in seen: continue
        seen.add(s["domain"]); uniq.append(s)
    print(f"{len(uniq)} sites", flush=True)
    rows = []
    with cf.ThreadPoolExecutor(3) as ex:
        for i, r in enumerate(ex.map(probe, uniq), 1):
            rows.append(r); print(f"[{i}/{len(uniq)}] " + json.dumps(r, ensure_ascii=False), flush=True)
    import csv
    with open(os.path.join(ROOT, "data", "rea_probe.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r})); w.writeheader(); w.writerows(rows)
    from collections import Counter
    print(Counter(r.get("verdict") for r in rows))

if __name__ == "__main__":
    main()
