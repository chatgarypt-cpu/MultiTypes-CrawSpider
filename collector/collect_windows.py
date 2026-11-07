#!/usr/bin/env python3
"""Date-window crawler for WordPress objects whose page-based pagination stalls.

  python3 collector/collect_windows.py --site <domain> --kind media [--from 2010-01] [--to 2026-12]
"""
import argparse, calendar, json, math, os, sys
from concurrent.futures import ThreadPoolExecutor
import requests, yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}

sys.path.insert(0, ROOT)
from crawler.mapping import map_wp_media, map_wp_post   # noqa: E402
from crawler.store import append_rows, load_known       # noqa: E402

def url_of(site, kind, after, before, page, per=100):
    ep = kind
    base = site["base"]
    if site.get("api") == "rest_route":
        q = f"/?rest_route=/wp/v2/{ep}&per_page={per}&page={page}&after={after}&before={before}"
    else:
        q = f"/wp-json/wp/v2/{ep}?per_page={per}&page={page}&after={after}&before={before}"
    return base + q

def fetch(site, kind, after, before, page, per=100):
    try:
        r = requests.get(url_of(site, kind, after, before, page, per), headers=UA, timeout=60)
        if r.status_code != 200: return [], 0
        d = r.json()
        total = int(r.headers.get("X-WP-Total") or 0)
        return (d if isinstance(d, list) else []), total
    except Exception:
        return [], 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True); ap.add_argument("--kind", default="media")
    ap.add_argument("--from", dest="start", default="2010-01"); ap.add_argument("--to", dest="end", default="2026-12")
    ap.add_argument("--suffix", default="")
    a = ap.parse_args()
    site = next(s for s in yaml.safe_load(open(os.path.join(ROOT, "sources.yaml"))) if s["domain"] == a.site)
    kind_out = a.kind + a.suffix
    known = load_known(a.site, kind_out) if a.suffix else load_known(a.site, a.kind)
    y0, m0 = map(int, a.start.split("-")); y1, m1 = map(int, a.end.split("-"))
    total_saved = 0
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            if (y, m) < (y0, m0) or (y, m) > (y1, m1): continue
            after = f"{y}-{m:02d}-01T00:00:00"
            last_day = calendar.monthrange(y, m)[1]
            before = f"{y}-{m:02d}-{last_day}T23:59:59"
            first, total = fetch(site, a.kind, after, before, 1)
            if not first:
                continue
            pages = max(1, math.ceil(total / 100)) if total else 1
            saved = 0

            def rows_of(batch):
                out = []
                for p in batch:
                    row = map_wp_media(p, site) if a.kind == "media" else map_wp_post(p, site)
                    u = row.get("url") or ""
                    if not u or u in known: continue
                    known.add(u); out.append(row)
                return out

            batches = [first]
            if pages > 1:
                with ThreadPoolExecutor(max_workers=6) as ex:
                    batches += list(ex.map(lambda pg: fetch(site, a.kind, after, before, pg)[0],
                                            range(2, pages + 1)))
            rows = []
            for b in batches: rows += rows_of(b)
            if rows:
                append_rows(a.site, rows, kind_out); saved = len(rows)
            total_saved += saved
            if saved: print(f"{y}-{m:02d} +{saved} (total {total_saved})", flush=True)
    print(f"done {a.site} {a.kind}: +{total_saved}")

if __name__ == "__main__":
    main()
