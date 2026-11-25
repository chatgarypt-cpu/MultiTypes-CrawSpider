#!/usr/bin/env python3
"""Deduplicate a site's article store by canonical URL.

  python3 collector/dedupe.py <domain> [<domain> ...]     # rewrite in place (atomic)
  python3 collector/dedupe.py --all                       # every site with data
  python3 collector/dedupe.py --all --dry-run             # report only, write nothing

Keeps the row with the longest body when a URL appears more than once.
"""
import glob, json, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crawler import config
from crawler.request import canonical_key
from crawler.store import collection_lock, _recover_append


def dedupe(domain, dry_run=False):
    directory = os.path.join(config.DATA, domain)
    if not os.path.isfile(os.path.join(directory, "articles.jsonl")):
        return None
    if dry_run:
        return _dedupe(domain, dry_run=True)
    with collection_lock(domain, data_dir=config.DATA):
        _recover_append(os.path.join(directory, "articles.jsonl"), os.path.join(directory, "urls.txt"))
        return _dedupe(domain, dry_run=False)


def _dedupe(domain, dry_run):
    path = os.path.join(config.DATA, domain, "articles.jsonl")
    if not os.path.exists(path):
        return None
    best, total = {}, 0
    for line in open(path, encoding="utf-8"):
        try:
            row = json.loads(line)
        except Exception:
            continue
        total += 1
        key = canonical_key(row.get("url") or "")
        if key not in best or len(row.get("text") or "") > len(best[key].get("text") or ""):
            best[key] = row
    stats = {"domain": domain, "before": total, "after": len(best), "removed": total - len(best)}
    if dry_run:
        stats["dry_run"] = True
        return stats
    tmp = tempfile.NamedTemporaryFile("w", delete=False, dir=os.path.dirname(path), encoding="utf-8")
    with tmp as fh:
        for row in best.values():
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    urls = os.path.join(config.DATA, domain, "urls.txt")
    with tempfile.NamedTemporaryFile("w", delete=False, dir=os.path.dirname(path), encoding="utf-8") as fh:
        for url in sorted(r["url"] for r in best.values() if r.get("url")):
            fh.write(url + "\n")
    os.replace(tmp.name, path)
    os.replace(fh.name, urls)
    return stats


if __name__ == "__main__":
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    if args[:1] == ["--all"]:
        domains = sorted({os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(config.DATA, "*", "articles.jsonl"))})
    else:
        domains = args
    for d in domains:
        r = dedupe(d, dry_run=dry_run)
        if r:
            print(json.dumps(r, ensure_ascii=False))
