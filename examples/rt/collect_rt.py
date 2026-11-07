#!/usr/bin/env python3
"""Collect RT metadata from its published RSS feeds; Python standard library only."""
import argparse
import csv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

FEEDS = {"all": "https://rt-english.com/rss/", "news": "https://rt-english.com/rss/news/"}
FIELDS = ["id", "title", "url", "published_at", "published_at_shanghai", "section", "author"]


def parse_feed(raw):
    root = ET.fromstring(raw)
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("Expected an RSS channel, not HTML/login/error content")
    items = channel.findall("item")
    if not items:
        raise ValueError("RSS returned no articles; investigate rather than claim success")
    rows = {}
    for item in items:
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("guid") or item.findtext("link") or "").strip()
        parsed = urlsplit(url)
        match = re.search(r"/(\d+)-", parsed.path)
        date = (item.findtext("pubDate") or "").strip()
        if not title or not match or not date or parsed.scheme != "https" or parsed.hostname != "rt-english.com":
            raise ValueError("Article identity/title/date differs from the verified RT feed format")
        timestamp = parsedate_to_datetime(date)
        if timestamp.tzinfo is None:
            raise ValueError("Publication date has no timezone")
        row = {"id": match.group(1), "title": title, "url": url,
               "published_at": timestamp.astimezone(timezone.utc).isoformat(),
               "published_at_shanghai": timestamp.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
               "section": parsed.path.strip("/").split("/")[0],
               "author": (item.findtext("{http://purl.org/dc/elements/1.1/}creator") or "").strip()}
        if row["id"] in rows and rows[row["id"]] != row:
            raise ValueError("Conflicting RSS records for the same article ID")
        rows[row["id"]] = row
    values = sorted(rows.values(), key=lambda row: row["published_at"], reverse=True)
    return values, {"channel": channel.findtext("title"), "raw_items": len(items),
                    "unique_items": len(values), "last_build_date": channel.findtext("lastBuildDate")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", choices=FEEDS, default="all")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.out.exists():
        parser.error("--out must be a new directory")
    request = Request(FEEDS[args.feed], headers={"User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml,application/xml"})
    with urlopen(request, timeout=20) as response:
        if urlsplit(response.url).hostname != "rt-english.com":
            raise ValueError("Feed redirected outside the selected host")
        raw = response.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("Unexpectedly large RSS response")
        http = {"http_status": response.status, "content_type": response.headers.get("Content-Type"), "final_url": response.url}
    rows, metadata = parse_feed(raw)
    selected = rows[:args.limit]
    # ponytail: RSS is a current window; historical backfill needs a separate verified source.
    metadata.update({"source_url": FEEDS[args.feed], "fetched_at": datetime.now(timezone.utc).isoformat(),
                     "source_sha256": hashlib.sha256(raw).hexdigest(), "requested": args.limit,
                     "collected": len(selected), "status": "target-reached" if len(selected) == args.limit else "feed-window-limited",
                     "historical_archive_complete": False, **http})
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "source.xml").write_bytes(raw)
    (args.out / "articles.json").write_text(json.dumps(selected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.out / "articles.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(selected)
    (args.out / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    main()
