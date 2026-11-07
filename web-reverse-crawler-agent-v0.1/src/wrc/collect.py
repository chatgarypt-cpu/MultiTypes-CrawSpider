import csv
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse
import httpx

FIELDS = ("id", "title", "url", "published_at")

def collect(api_url: str, db_path="outputs/articles.db", csv_path="outputs/articles.csv", limit=5, max_pages=10, delay=0.2):
    # v0.1 only permits the explicitly approved loopback demo target.
    parsed = urlparse(api_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != 8765 or parsed.path != "/api/articles":
        raise ValueError("v0.1 collector only permits the local demo API")
    articles = {}
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        for page in range(1, max_pages+1):
            r = client.get(api_url, params={"page": page, "limit": limit})
            r.raise_for_status()
            rows = r.json()["articles"]
            if not rows:
                break
            for row in rows:
                if all(isinstance(row.get(k), str) for k in FIELDS):
                    articles[row["id"]] = {k: row[k] for k in FIELDS}
            if len(rows) < limit:
                break
            time.sleep(delay)
    db = Path(db_path); csv_file = Path(csv_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY, title TEXT, url TEXT, published_at TEXT)")
        conn.executemany("INSERT OR REPLACE INTO articles VALUES (:id,:title,:url,:published_at)", articles.values())
    with csv_file.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(articles.values())
    return len(articles)
