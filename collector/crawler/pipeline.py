"""Row pipeline: normalise -> validate -> (store happens in store.append_rows)."""
import re
from urllib.parse import urlsplit, urlunsplit

from crawler.request import canonical_key

# 2026-11-12 规则：短正文不再丢弃，采元数据即可（url/title/date 等）。
# 仍保留 per-site min_text 覆盖能力；需要恢复质量门槛时在 sources.yaml 配 min_text。
MIN_TEXT = {"posts": 0, "pages": 0, "media": 0}


def threshold(site: dict | None, kind: str) -> int:
    if site and site.get("min_text") is not None:
        return int(site["min_text"])
    return MIN_TEXT.get(kind, 0)


def normalize(row: dict, site: dict | None = None) -> dict:
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


def validate(row: dict, site: dict, kind: str) -> tuple[bool, str]:
    """(ok, reason). Reasons are counters, so a run can explain its own drop rate."""
    if not row.get("url"):
        return False, "no_url"
    if not row.get("title") and not row.get("text"):
        return False, "empty"
    if len(row.get("text") or "") < threshold(site, kind):
        return False, "short_text"
    return True, ""


def dedupe_key(row: dict) -> str:
    return canonical_key(row.get("url") or "")
