"""Read-only samples and server-owned configuration for confirmed web jobs."""
from collections import OrderedDict
import os
import time
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4

import requests
from lxml import etree

from crawler import discover
from crawler.extract import extract_best
from crawler.htmlpage import page_error, paywall_signal, xpath_result
from crawler.mapping import map_wp_media, map_wp_post
from crawler.pipeline import normalize, validate
from crawler.web import Fetcher, parse_json_tolerant
from web_jobs import LOCK, normalize_url, submit
from web_worker import job_site, re_safe_kind, resolve_site

SAMPLE_SIZE = max(1, int(os.getenv("CRAWL_PREVIEW_SAMPLES", "3")))
PREVIEW_TTL = 15 * 60
PREVIEWS = OrderedDict()
EDITABLE_FIELDS = {"title", "created_at", "text"}
HTML_CHANNELS = {"html", "rss", "sitemap"}


class SampleFetcher(Fetcher):
    """Bound discovery independently of the full job and keep samples in order."""

    def __init__(self, site):
        super().__init__(site)
        self.remaining = max(1, int(os.getenv("CRAWL_PREVIEW_DISCOVERY_REQUESTS", "8")))

    def get_many(self, urls, timeout=12):
        selected = urls[:self.remaining]
        self.remaining -= len(selected)
        return super().get_many(selected, timeout=timeout)

    def stream_many(self, urls, workers=1):
        yield from self.get_many(urls)


def _rules(values):
    if not isinstance(values, dict) or values.keys() - EDITABLE_FIELDS:
        raise ValueError("仅支持修改标题、发布日期和正文字段")
    rules = {}
    for field, value in values.items():
        if not isinstance(value, str):
            raise ValueError("XPath 必须是文本")
        if value.strip():
            rules[field] = value.strip()
    return rules


def _issue(url, reason, status=None):
    return {"url": url, "reason": reason, "http_status": status}


def _sample(row, site, fields=None):
    row = normalize(row, site)
    valid, _ = validate(row, site, "posts")
    if not valid or not row.get("text"):
        return None
    return {
        "url": row["url"],
        "title": row.get("title") or "",
        "created_at": row.get("created_at") or "",
        "text": row["text"][:12000],
        "text_length": len(row["text"]),
        "fields": fields or {},
    }


def _html_sample(url, status, body, site, rules):
    if status != 200:
        return None, _issue(url, body if status == 0 and body else "fetch_failed", status)
    if not body or page_error(body):
        return None, _issue(url, page_error(body) or "no_body", status)
    if paywall_signal(body, site.get("premium_marker", "")):
        return None, _issue(url, "access_restricted", status)
    fields = {field: xpath_result(body, expr) for field, expr in rules.items()}
    for result in fields.values():
        result["value"] = result["value"][:12000]
    row = extract_best(url, body, site, min_text=0)
    sample = _sample(row, site, fields) if row else None
    return sample, None if sample else dict(_issue(url, "no_article", status), fields=fields)


WP_KINDS = {"posts": "文章 / 新闻", "pages": "页面", "media": "媒体元数据"}
SCOPE_LABELS = {"default": "按通道默认范围", "all": "通道可发现的全部内容", "section": "当前栏目", "path": "指定 URL 路径"}


def _scope_options(scope):
    if scope is None:
        scope = {}
    if not isinstance(scope, dict):
        raise ValueError("采集范围格式不正确")
    mode = scope.get("mode", "default")
    if not isinstance(mode, str) or mode not in SCOPE_LABELS:
        raise ValueError("请选择有效的采集范围")
    kinds = scope.get("kinds", [])
    if not isinstance(kinds, list) or any(not isinstance(kind, str) or not re_safe_kind(kind) for kind in kinds):
        raise ValueError("内容类型无效")
    path = scope.get("path", "")
    if not isinstance(path, str):
        raise ValueError("路径前缀必须是文本")
    path = path.strip().rstrip("/")
    if mode == "path" and (not path.startswith("/") or path.startswith("//")
                            or any(c in unquote(path) for c in "?#\\")
                            or any(part in (".", "..") for part in unquote(path).split("/"))):
        raise ValueError("请填写站内路径前缀，例如 /news")
    return mode, list(dict.fromkeys(kinds)), path


def _wordpress_samples(site, fetcher, kinds, result):
    for kind in kinds:
        api = discover.wp_page_url(dict(site, wp_per_page=SAMPLE_SIZE), 1, kind=kind)
        if not result["samples"]:
            result["source"] = api
        _, status, body = fetcher.get_many([api], timeout=12)[0]
        items = parse_json_tolerant(body) if status == 200 else None
        if not isinstance(items, list):
            result["issues"].append(_issue(api, "invalid_api_response" if status == 200 else "fetch_failed", status))
            continue
        count = 0
        for item in items[:SAMPLE_SIZE]:
            if not isinstance(item, dict) or (item.get("content") or {}).get("protected"):
                continue
            if kind == "media":
                row = map_wp_media(item, site)
                if not row["url"]:
                    continue
                sample = {"url": row["url"], "title": row["title"], "created_at": row["created_at"],
                          "text": "", "text_length": 0, "fields": {}, "mime": row["mime"]}
            else:
                sample = _sample(map_wp_post(item, site), site)
            if sample:
                result["samples"].append(dict(sample, kind=kind))
                count += 1
        if not count:
            result["issues"].append(_issue(api, "no_article" if kind == "posts" else "no_collection_sample", status))


def preview(value, raw_rules, channel="auto", selector="", scope=None):
    mode, selected, path = _scope_options(scope)
    url = normalize_url(value)
    if not isinstance(channel, str) or channel not in HTML_CHANNELS | {"wp-api", "auto"}:
        raise ValueError("请选择支持的采集通道")
    if mode == "path" and channel == "auto":
        channel = "html"
    if mode == "section":
        if channel not in ("auto", "html"):
            raise ValueError("当前栏目范围使用网页列表通道")
        channel = "html"
    if not isinstance(selector, str):
        raise ValueError("详情链接规则必须是文本")
    selector = selector.strip()
    if selector:
        if mode == "all":
            raise ValueError("全站遍历不使用自定义详情链接规则")
        if channel != "html":
            raise ValueError("详情链接规则仅用于网页列表通道")
        try:
            etree.XPath(selector)
        except etree.XPathSyntaxError as exc:
            raise ValueError("详情链接 XPath 语法有误") from exc
    rules = _rules(raw_rules)
    invalid = []
    for field, expr in rules.items():
        try:
            etree.XPath(expr)
        except etree.XPathSyntaxError:
            invalid.append(field)

    result = {"url": url, "channel": "", "transport": "", "source": "", "samples": [],
              "issues": [], "editable": False, "rules": rules, "preview_id": None, "available_kinds": []}
    try:
        site = job_site(resolve_site(url, check_entry=True, channel=channel))
    except (ValueError, OSError, requests.RequestException) as exc:
        result["issues"].append(_issue(url, str(exc)))
        return result

    automatic = channel == "auto"
    manual_html = channel == "html"
    if manual_html:
        site["list_url"] = site["start_url"]
        if selector:
            site["item_selector"] = selector
    channel = site["channel"]
    if selected and channel != "wp-api":
        raise ValueError("此通道只提供网页内容，选择页面或媒体请使用 WordPress REST")
    if mode == "path":
        if channel not in HTML_CHANNELS:
            raise ValueError("路径限定适用于网页列表、RSS 或站点地图")
        site["_path_scope"] = path
        site.pop("_automatic", None)
    if mode == "all" and channel in ("html", "rss"):
        site["_automatic"] = True
    selection = {"mode": mode, "label": SCOPE_LABELS[mode] + (f" · {path}" if mode == "path" else ""),
                 "kinds": selected}
    if mode != "default" or selected:
        site["_selection"] = selection
        site["_restricted_scope"] = mode in ("section", "path") or bool(selected)
    result["selection"] = selection
    transport = site.get("via") or "direct"
    entry = site.get("start_url") or site["base"]
    source = {
        "html": site.get("list_url") or entry,
        "rss": site.get("feed") or "/feed/",
        "sitemap": site.get("sitemap") or "/sitemap.xml",
    }.get(channel, entry)
    result.update(channel=channel, transport=transport, source=urljoin(site["base"].rstrip("/") + "/", source),
                  editable=channel in HTML_CHANNELS and transport == "direct",
                  rules={field: expr for field, expr in {**(site.get("schema") or {}), **rules}.items()
                         if field in EDITABLE_FIELDS})
    if invalid:
        result["issues"].extend(dict(_issue(url, "invalid_xpath"), field=field) for field in invalid)
        return result
    if transport != "direct" or channel not in HTML_CHANNELS | {"wp-api"}:
        result["issues"].append(_issue(entry, "preview_unavailable"))
        return result
    if rules and automatic and channel == "wp-api":
        return preview(url, rules, "html", scope=scope)
    if rules and not result["editable"]:
        raise ValueError("此通道使用结构化字段，无法应用网页 XPath")
    if rules:
        site["schema"] = {**(site.get("schema") or {}), **rules}

    # The preview only requests public URLs on the resolved site. Its settings
    # never override transport, paging or concurrency in the full job.
    fetch_site = dict(site, documents=False, _public_host=urlsplit(normalize_url(site["base"])).netloc)
    samples, issues = result["samples"], result["issues"]
    fetcher = Fetcher(fetch_site)
    if channel == "wp-api":
        inventory = discover.wp_type_inventory(site, fetcher)
        options = dict(WP_KINDS)
        types = dict(site.get("wp_types") or {})
        for kind in types:
            options.setdefault(kind, kind)
        for item in inventory.get("types", []):
            kind = item["type"]
            if kind not in ("post", "page", "attachment") and re_safe_kind(kind) and not kind.startswith(("wp_", "nav_")):
                options[kind] = item["name"] or kind
                types[kind] = item["route"]
        result["available_kinds"] = [{"value": kind, "label": label} for kind, label in options.items()]
        if set(selected) - options.keys():
            raise ValueError("所选内容类型在该站点未公开")
        if selected:
            site["_scope_kinds"] = selected
            site["wp_types"] = types
        _wordpress_samples(site, fetcher, selected or ["posts"], result)
    else:
        discovery = SampleFetcher(fetch_site)
        discovery_failures = []
        sample_site = dict(site, pages=1)
        if channel == "html":
            if manual_html and not selector:
                # Prefer links explicitly marked as article detail over URL guesses.
                read_more = ("//a[contains(concat(' ', normalize-space(../@class), ' '),"
                             " ' read-more ')]/@href")
                urls = discover.html_urls(dict(sample_site, item_selector=read_more),
                                          discovery, SAMPLE_SIZE, discovery_failures)
                if urls and mode != "all":
                    site["item_selector"] = read_more
                elif not urls and not discovery_failures:
                    urls = discover.html_urls(sample_site, discovery, SAMPLE_SIZE, discovery_failures)
            else:
                urls = discover.html_urls(sample_site, discovery, SAMPLE_SIZE, discovery_failures)
            # A pasted article takes precedence over links found on that page.
            start = site.get("start_url") or url
            if (not manual_html and urlsplit(start).path != "/") or not (urls or discovery_failures or selector):
                if manual_html and not urls and discover.in_path_scope(start, site):
                    site["seed_urls"] = [start]
                urls = list(dict.fromkeys([start, *urls]))
        elif channel == "rss":
            urls = discover.rss_urls(sample_site, discovery, SAMPLE_SIZE, discovery_failures)
        else:
            urls = discover.sitemap_urls(sample_site, discovery, SAMPLE_SIZE, discovery_failures)
        issues.extend(_issue(f["url"], f.get("error") or "discovery_failed", f["status"])
                      for f in discovery_failures)
        if not urls and not issues:
            issues.append(_issue(entry, "no_links"))
        for detail in urls:
            if not discover.in_path_scope(detail, site):
                continue
            if len(samples) >= SAMPLE_SIZE:
                break
            _, status, body = fetcher.get_many([detail], timeout=12)[0]
            sample, issue = _html_sample(detail, status, body, site, result["rules"])
            if sample:
                samples.append(sample)
            if issue and not (len(urls) > 1 and detail == entry and issue["reason"] == "no_article"):
                issues.append(issue)

    if automatic and not selected and channel == "wp-api" and not samples and any(
        issue["reason"] == "no_article" for issue in issues
    ):
        fallback = preview(url, rules, "html", scope=scope)
        fallback["issues"].insert(0, _issue(result["source"], "auto_wp_no_article", 200))
        return fallback

    unmatched = [
        field for field in rules
        if not any(sample["fields"].get(field, {}).get("status") == "matched" for sample in samples)
    ]
    if samples and unmatched:
        issues.extend(dict(_issue(entry, "rule_no_sample_match"), field=field) for field in unmatched)
    if samples and not unmatched:
        preview_id = uuid4().hex
        with LOCK:
            now = time.monotonic()
            while PREVIEWS and (len(PREVIEWS) >= 64 or next(iter(PREVIEWS.values()))[0] <= now):
                PREVIEWS.popitem(last=False)
            PREVIEWS[preview_id] = (now + PREVIEW_TTL, url, site)
        result["preview_id"] = preview_id
    return result


def confirm(preview_id):
    if not isinstance(preview_id, str):
        raise ValueError("请先完成试采")
    with LOCK:
        entry = PREVIEWS.get(preview_id)
        if entry is None or entry[0] <= time.monotonic():
            PREVIEWS.pop(preview_id, None)
            raise ValueError("试采已过期，请重新试采")
        _, url, site = entry
        results = submit([url], site=site)
        if any(r["status"] == "queued" for r in results):
            PREVIEWS.pop(preview_id, None)
        return results
