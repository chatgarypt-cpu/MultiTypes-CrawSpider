"""HTML listing channel: listing page(s) + link_pattern -> article URLs -> fetch bodies."""
from crawler.discover import html_urls
from crawler.channels.base import collect_urls


def run(site, limit, log, fetcher, kind="posts"):
    cap = int(site.get("discover_cap") or 0)
    urls = html_urls(site, fetcher, cap)
    return collect_urls(site, urls, limit, log, fetcher, kind)
