"""Row pipeline: normalise -> validate -> (store happens in store.append_rows)."""
import re
from urllib.parse import urlsplit, urlunsplit

from crawler.request import canonical_key

# per-kind body threshold: media rows carry no article text, pages are often short
MIN_TEXT = {"posts": 120, "pages": 60, "media": 0}


def threshold(site, kind):
    if site and site.get("min_text") is not None:
        return int(site["min_text"])
    return MIN_TEXT.get(kind, 60)


def normalize(row, site=None):
    """Trim whitespace, absolutise URLs, fill host; never invent content."""
    for k in ("title", "text", "description", "author", "tag", "created_at"):
        if isinstance(row.get(k), str):
            row[k] = row[k].strip()
    url = (row.get("url") or "").strip()
    if url and site:
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/") and site.get("base"):
            url = site["base"].rstrip("/") + url
        p = urlsplit(url)
        if p.netloc:
            url = urlunsplit((p.scheme or "https", p.netloc, p.path, p.query, ""))
    row["url"] = url
    if not row.get("host") and url:
        row["host"] = urlsplit(url).netloc
    return row


def validate(row, site, kind):
    """(ok, reason). Reasons are counters, so a run can explain its own drop rate."""
    if not row.get("url"):
        return False, "no_url"
    if not row.get("title") and not row.get("text"):
        return False, "empty"
    if len(row.get("text") or "") < threshold(site, kind):
        return False, "short_text"
    return True, ""


def dedupe_key(row):
    return canonical_key(row.get("url") or "")
