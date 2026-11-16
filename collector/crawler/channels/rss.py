"""RSS channel: feed items -> article URLs -> fetch bodies."""
from crawler.discover import rss_urls
from crawler.channels.base import collect_urls


def run(site, limit, log, fetcher, kind="posts"):
    cap = int(site.get("discover_cap") or 0)
    failures = []
    urls = rss_urls(site, fetcher, cap, failures=failures)
    return collect_urls(site, urls, limit, log, fetcher, kind, discovery_failures=failures)
