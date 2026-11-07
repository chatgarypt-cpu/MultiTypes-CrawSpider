"""Ad-hoc crawling: point at a URL and let the crawler figure out how to read the site.

Used by `collect.py --url <article>` and `collect.py --site-url <homepage>`.
"""
import json, os, re
from urllib.parse import urlsplit

import requests

from crawler.config import DATA, UA
try:
    from curl_cffi import requests as cffi_requests
except Exception:
    cffi_requests = None

SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/news-sitemap.xml"]
FEED_PATHS = ["/feed/", "/rss.xml", "/atom.xml", "/index.php?option=com_content&view=featured&format=feed&type=rss"]


def _get(url, timeout=20, impersonate=False):
    if impersonate and cffi_requests is not None:
        return cffi_requests.get(url, impersonate="chrome", timeout=timeout)
    return requests.get(url, headers={"User-Agent": UA}, timeout=timeout)


def _looks_blocked(r):
    return r.status_code in (403, 503) or "just a moment" in (r.text or "")[:2000].lower()


def detect(url):
    """Probe a site and return a sources.yaml-style site dict (plus 'detected' notes)."""
    p = urlsplit(url if "://" in url else "https://" + url)
    base = f"{p.scheme}://{p.netloc}"
    domain = p.netloc
    notes = []
    imp = False

    # 1. WordPress REST (cheapest: gives exact totals + full bodies)
    for path in ("/wp-json/wp/v2/posts?per_page=1", "/?rest_route=/wp/v2/posts&per_page=1"):
        try:
            r = _get(base + path)
            if r.status_code == 200 and r.headers.get("X-WP-Total"):
                return {"domain": domain, "base": base, "channel": "wp-api",
                        "via": "impersonate" if imp else "direct", "detected": {"wp_total": r.headers.get("X-WP-Total")}}
            if _looks_blocked(r):
                r2 = _get(base + path, impersonate=True)
                if r2.status_code == 200 and r2.headers.get("X-WP-Total"):
                    imp = True
                    return {"domain": domain, "base": base, "channel": "wp-api", "via": "impersonate",
                            "detected": {"wp_total": r2.headers.get("X-WP-Total")}}
        except Exception as e:
            notes.append(f"wp-json: {str(e)[:60]}")

    # 2. sitemap
    for path in SITEMAP_PATHS:
        try:
            r = _get(base + path)
            if r.status_code == 200 and ("<urlset" in r.text[:3000] or "<sitemapindex" in r.text[:3000]):
                return {"domain": domain, "base": base, "channel": "sitemap", "sitemap": path,
                        "via": "impersonate" if imp else "direct", "detected": {"sitemap": path}}
        except Exception:
            pass

    # 3. RSS / Atom
    for path in FEED_PATHS:
        try:
            r = _get(base + path)
            if r.status_code == 200 and re.search(r"<(rss|feed|rdf:RDF)[\s>]", r.text[:2000]):
                return {"domain": domain, "base": base, "channel": "rss", "feed": path,
                        "via": "impersonate" if imp else "direct", "detected": {"feed": path}}
        except Exception:
            pass

    # 4. plain HTML listing (the page you gave us becomes the listing page)
    return {"domain": domain, "base": base, "channel": "html", "list_url": url,
            "link_pattern": r'/[^"\']*(?:/20\d\d/|-\d{4,}\.html|/news/|/article)', "via": "direct",
            "detected": {"list_url": url, "link_pattern": "heuristic"}, "notes": notes}


def remember(site):
    """Persist an auto-detected site so repeat runs reuse the same rule."""
    path = os.path.join(DATA, "adhoc_sites.json")
    try:
        data = json.load(open(path))
    except Exception:
        data = {}
    data[site["domain"]] = {k: v for k, v in site.items() if k != "notes"}
    json.dump(data, open(path, "w"), ensure_ascii=False, indent=1)


def collect_one(url, limit=1):
    """Fetch a single article URL and store it under its host."""
    from crawler.extract import extract_best
    from crawler.pipeline import normalize, validate
    from crawler.store import append_rows, load_known, save_cursor
    from crawler.web import Fetcher
    import export_csv

    p = urlsplit(url)
    base, domain = f"{p.scheme}://{p.netloc}", p.netloc
    site = {"domain": domain, "base": base, "channel": "html"}
    fetcher = Fetcher(site)
    out = []
    for u, status, body in fetcher.stream_many([url], workers=1):
        if status != 200 or not body:
            return {"url": url, "error": f"http {status}"}
        row = extract_best(url, body, site, min_text=60)
        if not row:
            return {"url": url, "error": "no article text extracted"}
        row = normalize(row, site)
        ok, why = validate(row, site, "posts")
        if not ok:
            return {"url": url, "error": why}
        known = load_known(domain, "posts")
        duplicate = row["url"] in known
        if not duplicate:
            append_rows(domain, [row], "posts")
            out.append(row)
        save_cursor(domain, {"next_page": 1, "total_pages": 1, "saved": len(out), "done": True}, "posts")
        result = {"url": url, "saved": 0 if duplicate else 1, "duplicate": duplicate,
                  "title": row.get("title", ""), "text_len": len(row.get("text") or ""),
                  "created_at": row.get("created_at", "")}
    try:
        export_csv.export(os.path.join(DATA, domain), os.path.join(DATA, "csv", domain + ".csv"), kind="posts")
    except Exception:
        pass
    return result
