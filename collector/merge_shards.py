#!/usr/bin/env python3
"""Merge articles<sfx>.jsonl / urls<sfx>.txt shards into the site's main files."""
import glob, json, os, sys

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

def merge(domain):
    d = os.path.join(DATA, domain)
    main_jl = os.path.join(d, "articles.jsonl"); main_uf = os.path.join(d, "urls.txt")
    seen = set()
    if os.path.exists(main_uf):
        seen = {l.strip() for l in open(main_uf) if l.strip()}
    added = 0
    with open(main_jl, "a") as w, open(main_uf, "a") as wu:
        for jl in sorted(glob.glob(os.path.join(d, "articles?.jsonl"))):
            for line in open(jl):
                try: r = json.loads(line)
                except Exception: continue
                u = r.get("url")
                if not u or u in seen: continue
                seen.add(u); w.write(json.dumps(r, ensure_ascii=False) + "\n"); wu.write(u + "\n"); added += 1
    print(f"{domain}: merged +{added}, total urls {len(seen)}")

if __name__ == "__main__":
    for dom in sys.argv[1:]:
        merge(dom)
