#!/usr/bin/env python3
"""Export each site's JSONL into its own 31-column CSV (UTF-8 BOM, CRLF, quoted).

  python3 collector/export_csv.py --all              # data/<domain>/articles.csv for every site
  python3 collector/export_csv.py --site <domain>    # one site
"""
import argparse, csv, glob, json, os
from crawler.config import stored_kinds

MEDIA_COLS = ["id","title","url","link","mime","width","height","alt","caption","description","created_at","parent","host"]

COLS = ["title","sub_title","url","channel_text","country_text","lang_text","user_id","user_author_name","user_url",
        "created_at","description","text","image","video","link","view_count","like_count","reply_count",
        "retweet_count","dislike_count","favourite_count","tag","keyword","host","first_channel","second_channel",
        "third_channel","editor","updated_at","hit_num","read_num"]

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CSV_DIR = os.path.join(DATA, "csv")          # one flat file per site: <域名>.csv

def stream(jsonl):
    with open(jsonl, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except Exception:      # last line may be half-written while the crawler runs
                continue

def export(site_dir, out_path, kind="posts"):
    jsonl = os.path.join(site_dir, "articles.jsonl" if kind == "posts" else f"{kind}.jsonl")
    cols = MEDIA_COLS if kind == "media" else COLS
    if not os.path.exists(jsonl): return 0
    n = 0
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in stream(jsonl):
            w.writerow(row); n += 1
    return n

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--site")
    a = ap.parse_args()
    dirs = sorted(d for d in glob.glob(os.path.join(DATA, "*")) if os.path.isdir(d))
    os.makedirs(CSV_DIR, exist_ok=True)
    if a.site:
        d = os.path.join(DATA, a.site)
        out = os.path.join(CSV_DIR, a.site + ".csv")
        n = export(d, out)
        print(f"{a.site}: {n} rows -> {out} ({os.path.getsize(out)/1e6:.1f} MB)")
        return
    if not a.all:
        ap.error("need --all or --site")
    total = 0
    for d in dirs:
        name = os.path.basename(d)
        parts = []
        for kind in stored_kinds(d):
            suffix = "" if kind == "posts" else "." + kind
            if not os.path.exists(os.path.join(d, "articles.jsonl" if kind == "posts" else f"{kind}.jsonl")):
                continue
            n = export(d, os.path.join(CSV_DIR, name + suffix + ".csv"), kind)
            total += n
            parts.append(f"{kind}={n}")
        if parts:
            print(f"  {name:<32} " + "  ".join(parts))
    print(f"exported {total} rows across {len(dirs)} sites")

if __name__ == "__main__":
    main()
