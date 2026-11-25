#!/usr/bin/env python3
"""Export each site's JSONL into its own 31-column CSV (UTF-8 BOM, CRLF, quoted).

  python3 collector/export_csv.py --all              # data/<domain>/articles.csv for every site
  python3 collector/export_csv.py --site <domain>    # one site
"""
import argparse, csv, fcntl, glob, json, os
from pathlib import Path
from tempfile import TemporaryDirectory
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
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                if not line.endswith("\n"):       # concurrent writer's unfinished final record
                    return
                raise ValueError(f"{jsonl}:{line_no}: invalid JSON record") from exc

def export(site_dir, out_path, kind="posts", *, only_if_stale=False):
    source = Path(site_dir) / ("articles.jsonl" if kind == "posts" else f"{kind}.jsonl")
    target = Path(out_path)
    if not source.is_file():
        return 0
    cols = MEDIA_COLS if kind == "media" else COLS
    with target.with_name("." + target.name + ".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = source.stat()
        signature = f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"
        if only_if_stale and target.is_file():
            lock.seek(0)
            if lock.read() == signature + ":" + str(target.stat().st_mtime_ns):
                return None
        with TemporaryDirectory(dir=target.parent) as temp:
            pending = Path(temp) / target.name
            with pending.open("w", encoding="utf-8-sig", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=cols, extrasaction="ignore")
                writer.writeheader()
                count = 0
                for row in stream(source):
                    writer.writerow(row)
                    count += 1
            if target.is_file():
                pending.chmod(target.stat().st_mode & 0o777)
            os.replace(pending, target)
            lock.seek(0)
            lock.truncate()
            lock.write(signature + ":" + str(target.stat().st_mtime_ns))
            lock.flush()
        return count


def refresh_download(data: Path, filename: str) -> None:
    """Refresh a site's named export from JSONL when a monitor download is requested."""
    if not filename.endswith(".csv") or Path(filename).name != filename:
        return
    stem = filename[:-4]
    domain, _, kind = stem.rpartition(".")
    for name, collection in ((stem, "posts"), (domain, kind)):
        if not name or name.startswith(".") or not collection:
            continue
        directory = data / name
        source = directory / ("articles.jsonl" if collection == "posts" else collection + ".jsonl")
        if directory.is_symlink() or source.is_symlink() or not source.is_file():
            continue
        csv_dir = data / "csv"
        csv_dir.mkdir(exist_ok=True)
        export(directory, csv_dir / filename, collection, only_if_stale=True)
        return

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
