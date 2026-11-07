#!/usr/bin/env python3
"""Sharded sitemap crawler: crawl one slice of a paginated sitemap into its own files.

  python3 collector/collect_shard.py --site www.lnc.nc --sitemap "https://www.lnc.nc/sitemap.xml?page={page}" \
      --from 1 --to 3 --suffix a

Writes data/<domain>/articles<sfx>.jsonl and data/<domain>/urls<sfx>.txt.
Merge afterwards with collector/merge_shards.py.
"""
import argparse, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from crawler.web import Fetcher, UA                      # noqa: E402
from crawler.extract import extract_article             # noqa: E402
from crawler.store import append_rows                   # noqa: E402
import json, requests                                   # noqa: E402
import yaml

DATA = os.path.join(os.path.dirname(ROOT), "data")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--sitemap", default=None)
    ap.add_argument("--from", dest="start", type=int, default=1)
    ap.add_argument("--to", dest="end", type=int, default=1)
    ap.add_argument("--suffix", default="a")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    sites = yaml.safe_load(open(os.path.join(ROOT, "sources.yaml")))
    site = next(s for s in sites if s["domain"] == a.site)
    tmpl = a.sitemap or (site["base"].rstrip("/") + (site.get("sitemap") or "/sitemap.xml") + "?page={page}")

    out_dir = os.path.join(DATA, a.site)
    os.makedirs(out_dir, exist_ok=True)
    jl = os.path.join(out_dir, f"articles{a.suffix}.jsonl")
    uf = os.path.join(out_dir, f"urls{a.suffix}.txt")

    urls = []
    for p in range(a.start, a.end + 1):
        u = tmpl.format(page=p)
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=60)
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
        except Exception as e:
            print(f"shard {a.suffix}: sitemap {p} failed {str(e)[:60]}", flush=True)
            continue
        locs = [l for l in locs if not re.search(r"\.(jpg|jpeg|png|gif|css|js|pdf|xml)$", l, re.I)]
        urls.extend(locs)
        print(f"shard {a.suffix}: page {p} +{len(locs)} urls", flush=True)

    seen = set()
    if os.path.exists(uf):
        seen = {l.strip() for l in open(uf) if l.strip()}
    urls = [u for u in dict.fromkeys(urls) if u not in seen]
    if a.limit: urls = urls[:a.limit]
    print(f"shard {a.suffix}: {len(urls)} urls to fetch", flush=True)

    fetcher = Fetcher(site)
    done = 0
    t0 = time.time()
    with open(jl, "a") as jf, open(uf, "a") as ufw:
        for u, status, body in fetcher.stream_many(urls, workers=a.workers):
            done += 1
            if status == 200 and body:
                row = extract_article(u, body, site)
                if row.get("text") and len(row["text"]) >= 120:
                    jf.write(json.dumps(row, ensure_ascii=False) + "\n"); jf.flush()
                    ufw.write(u + "\n"); ufw.flush()
            if done % 500 == 0:
                print(f"shard {a.suffix}: {done}/{len(urls)} {done/max(1,time.time()-t0):.1f}/s", flush=True)
    print(f"shard {a.suffix}: done {done}", flush=True)


if __name__ == "__main__":
    main()
