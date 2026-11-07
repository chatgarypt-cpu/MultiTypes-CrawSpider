"""Sitemap channel: stream sitemap URLs into the request queue, then fetch bodies."""
from crawler.discover import iter_sitemap_urls
from crawler.channels.base import collect_urls
from crawler.request import RequestQueue
from crawler.store import load_known

CHUNK = 5000


def run(site, limit, log, fetcher, kind="posts"):
    cap = int(site.get("discover_cap") or 0)
    q = RequestQueue(site["domain"], kind)
    q.seed_done(load_known(site["domain"], kind), channel=site.get("channel", ""))
    buf = []
    for u in iter_sitemap_urls(site, fetcher, cap):
        buf.append(u)
        if len(buf) >= CHUNK:
            q.add_many(buf, channel="sitemap"); buf = []
    if buf:
        q.add_many(buf, channel="sitemap")
    return collect_urls(site, [], limit, log, fetcher, kind)
