"""Sitemap channel: stream sitemap URLs into the request queue, then fetch bodies."""
from crawler.discover import iter_sitemap_urls
from crawler.channels.base import collect_urls
from crawler.request import RequestQueue

CHUNK = 5000


def run(site, limit, log, fetcher, kind="posts"):
    cap = int(site.get("discover_cap") or 0)
    q = RequestQueue(site["domain"], kind)
    failures = []                     # 非 200 的 sitemap 入口（子图 503 等），交给 base 判状态
    buf = []
    discovered = 0
    for u in iter_sitemap_urls(site, fetcher, cap, failures=failures):
        buf.append(u); discovered += 1
        if len(buf) >= CHUNK:
            q.add_many(buf, channel="sitemap"); buf = []
            log.info("sitemap progress domain=%s discovered=%d", site["domain"], discovered)
    if buf:
        q.add_many(buf, channel="sitemap")
    return collect_urls(site, [], limit, log, fetcher, kind,
                        discovered_count=discovered, discovery_failures=failures)
