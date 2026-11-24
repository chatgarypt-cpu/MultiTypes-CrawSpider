#!/usr/bin/env python3
"""Compare live WordPress totals with what is on disk; independent of cursors."""
import json, os, sys
import requests, yaml

from crawler.config import sources_path

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}

def total(base, kind, api):
    path = f"/wp-json/wp/v2/{kind}?per_page=1" if api != "rest_route" else f"/?rest_route=/wp/v2/{kind}&per_page=1"
    try:
        r = requests.get(base + path, headers=UA, timeout=40)
        return int(r.headers.get("X-WP-Total") or 0) if r.status_code == 200 else None
    except Exception:
        return None

def rows(domain, kind):
    f = os.path.join(DATA, domain, "urls.txt" if kind == "posts" else f"{kind}.urls.txt")
    return sum(1 for _ in open(f)) if os.path.exists(f) else 0

def main():
    sites = [s for s in yaml.safe_load(open(sources_path(ROOT)))
             if s["channel"] == "wp-api"]
    gaps = []
    for s in sites:
        d = s["domain"]
        for kind in ("posts", "pages", "media"):
            exp = total(s["base"], kind, s.get("api"))
            got = rows(d, kind)
            if exp is None:
                gaps.append({"domain": d, "kind": kind, "expected": None, "got": got, "note": "api unavailable"})
            elif exp and got < exp * 0.95:
                gaps.append({"domain": d, "kind": kind, "expected": exp, "got": got,
                             "missing": exp - got, "ratio": round(got / exp, 3)})
    gaps.sort(key=lambda g: (g.get("missing") or 0), reverse=True)
    json.dump(gaps, open(os.path.join(DATA, "audit_gaps.json"), "w"), ensure_ascii=False, indent=1)
    print(f"{len(gaps)} gaps")
    for g in gaps[:40]:
        print(f"  {g['domain']:<32} {g['kind']:<6} expected={g.get('expected')} got={g['got']} missing={g.get('missing')}")

if __name__ == "__main__":
    main()
