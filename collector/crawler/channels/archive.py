"""逆向采集通道（channel: archive）：Wayback CDX 发现 + 存档取文。

正向采集依赖站点自己暴露入口（sitemap / RSS / 栏目页 / JSON 接口）；站点下线、全站 403、
或接口改版之后这条路就断了。逆向采集反过来从"别人已经存下来的副本"入手：

1. 发现层：archive.org CDX 接口给出该域历史上所有 status=200 的 HTML 页面；
2. 取文层：`Fetcher` 配 `via: archive` 时走 `paywall.archive_fetch()`（wayback `id_`
   原始快照 → archive.today），不碰源站；
3. 提取层：仍用 trafilatura + `schema:` 字段，与其它通道完全一致。

配置（`cdx:` 块可选，缺省用 cdx_seed 的默认过滤）：

    - domain: example.org
      channel: archive
      via: archive
      base: https://example.org
      cdx:
        limit: 5000
        since: 2015
        match: "2020|2021"        # 只保留 URL 命中此正则的页面

已经用 `cdx_seed.py` 手工灌过队列的站，这里检测到 unprocessed 就不再重复拉 CDX。
"""
import re

from crawler.channels.base import collect_urls
from crawler.request import RequestQueue


def discover(site, log=None, sample_limit=None):
    """CDX 发现：返回 (urls, failures)。队列里已有待办时直接返回空列表。"""
    domain = site["domain"]
    kind = site.get("kind") or "posts"
    if sample_limit is None and RequestQueue(domain, kind).stats().get("unprocessed"):
        if log:
            log.info("archive channel: queue already has pending URLs, skip CDX")
        return [], []
    import cdx_seed
    cfg = site.get("cdx") or {}
    options = {"retries": 1, "timeout": 12} if sample_limit is not None else {}
    try:
        limit = int(cfg.get("limit") or 5000)
        urls = cdx_seed.cdx_urls(domain,
                                 limit=min(limit, sample_limit) if sample_limit is not None else limit,
                                 since=cfg.get("since"),
                                 pages_only=cfg.get("pages_only", True), **options)
    except Exception as exc:
        return [], [{"url": f"cdx:{domain}", "status": 0, "error": str(exc)[:160]}]
    pattern = cfg.get("match")
    if pattern:
        try:
            rx = re.compile(pattern)
            urls = [u for u in urls if rx.search(u)]
        except re.error as exc:
            return [], [{"url": f"cdx:{domain}", "status": 0, "error": f"bad match regex: {exc}"}]
    if log:
        log.info("archive channel: CDX discovered %d URLs for %s", len(urls), domain)
    return urls, []


def run(site, limit, log, fetcher, kind="posts"):
    urls, failures = discover(site, log)
    q = RequestQueue(site["domain"], kind)
    if urls:
        q.add_many(urls, channel="archive")
    return collect_urls(site, [], limit, log, fetcher, kind,
                        discovered_count=len(urls), discovery_failures=failures)
