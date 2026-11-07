"""Reconcile the durable request queue with the URL store on disk.

When a run is killed, when `mark_error` used to reset a whole site (the 2026-11-07
matangitonga regression), or when URLs were imported by a tool that bypassed the
queue, state.db can still list rows as `unprocessed` although their URL is already
in `urls.txt`. This module flips exactly those rows to `done`, so the next run does
not fetch and store them again.

    reconcile(domain=None, dry_run=False) -> {"updated": n, "by_domain": {domain: n}}
"""
import glob, os, sqlite3, time

from crawler import config
from crawler.request import canonical_key


def _url_files(domain_dir):
    """kind -> urls.txt path for one site dir. posts uses `urls.txt`, others `<kind>.urls.txt`."""
    out = {}
    posts = os.path.join(domain_dir, "urls.txt")
    if os.path.isfile(posts):
        out["posts"] = posts
    suffix = ".urls.txt"
    for path in glob.glob(os.path.join(domain_dir, "*" + suffix)):
        kind = os.path.basename(path)[:-len(suffix)]
        if kind:
            out[kind] = path
    return out


def _keys_on_disk(path):
    keys = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                url = line.strip()
                if url:
                    keys.add(canonical_key(url))
    except OSError:
        return set()
    return keys


def reconcile(domain=None, dry_run=False):
    """Mark `unprocessed` queue rows whose canonical URL already exists in urls.txt as done.

    Returns {"updated": n, "by_domain": {domain: n}}. `dry_run` counts but writes nothing.
    """
    result = {"updated": 0, "by_domain": {}}
    data_dir = config.DATA
    db_path = config.DB
    if not os.path.exists(db_path):
        return result
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        if domain:
            domains = [domain]
        else:
            domains = [r[0] for r in conn.execute(
                "SELECT DISTINCT domain FROM requests WHERE state='unprocessed'")]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for dom in sorted(set(domains)):
            url_files = _url_files(os.path.join(data_dir, dom))
            updated = 0
            for kind, path in sorted(url_files.items()):
                known = _keys_on_disk(path)
                if not known:
                    continue
                pending = [k for (k,) in conn.execute(
                    "SELECT key FROM requests WHERE domain=? AND kind=? AND state='unprocessed'",
                    (dom, kind)) if k in known]
                if not pending:
                    continue
                if dry_run:
                    updated += len(pending)
                else:
                    before = conn.total_changes
                    conn.executemany(
                        "UPDATE requests SET state='done', updated=? "
                        "WHERE domain=? AND kind=? AND key=? AND state='unprocessed'",
                        [(now, dom, kind, key) for key in pending])
                    updated += conn.total_changes - before
            if updated:
                result["by_domain"][dom] = updated
                result["updated"] += updated
        if not dry_run:
            conn.commit()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        # no requests table yet: nothing to reconcile
        conn.rollback()
    finally:
        conn.close()
    return result
