"""Read-only samples and server-owned configuration for confirmed web jobs."""
from collections import OrderedDict
import os
import re
import time
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit
from uuid import uuid4

import requests
import yaml
from lxml import etree

from crawler import capture
from crawler import settings
from crawler import contract
from crawler import discover
from crawler import paywall
from crawler.channels import archive, json_api, json_list
from crawler.config import UA
from crawler.extract import extract_best
from crawler.htmlpage import page_error, paywall_signal, xpath_result
from crawler.mapping import map_json_item, map_wp_media, map_wp_post
from crawler.pipeline import normalize, validate
from crawler.schema import validate_site
from crawler.web import Fetcher, get_text, parse_json_tolerant
from web_jobs import LOCK, normalize_url, submit
from web_worker import job_site, re_safe_kind, resolve_site

SAMPLE_SIZE = max(1, int(os.getenv("CRAWL_PREVIEW_SAMPLES", "3")))
PREVIEW_TTL = 15 * 60
PREVIEWS = OrderedDict()
EDITABLE_FIELDS = {"title", "created_at", "text"}
HTML_CHANNELS = {"html", "rss", "sitemap"}
API_CHANNELS = {"json-api", "json-list"}

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


def _html_sample(url, status, body, site, rules, retried=False):
    # 预览面板同样走站点开关的 13ft 回退：403/挑战页/付费墙命中时换一份正文再看
    fetch_failed = status != 200
    restricted = bool(body) and bool(paywall_signal(body, site.get("premium_marker", "")))
    if (fetch_failed or restricted or (body and page_error(body))) and not retried and paywall.enabled(site):
        new_status, new_body, source = paywall.recover(
            url, site, body if isinstance(body, str) else "", status)
        if source:
            return _html_sample(url, new_status, new_body, site, rules, retried=True)
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


def _api_site(site, api):
    if not isinstance(api, dict):
        raise ValueError("请填写接口路径")
    endpoint = api.get("path")
    if not isinstance(endpoint, str) or not endpoint.startswith("/") or endpoint.startswith("//"):
        raise ValueError("请输入同站接口路径，例如 /api/articles")
    parts = urlsplit(endpoint)
    decoded = unquote(parts.path)
    if (parts.netloc or "#" in endpoint or decoded.startswith("//")
            or re.search(r"[\\\s?#{}]", decoded)
            or any(segment in (".", "..") for segment in decoded.split("/"))):
        raise ValueError("接口路径格式不正确")
    pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if _portable_query_key(key)]
    if site["channel"] == "json-api":
        return dict(site, api=parts.path, api_query=urlencode(pairs))

    param = api.get("page_param", "page")
    if not isinstance(param, str) or not re.fullmatch(r"[\w.\[\]-]+", param) or not _portable_query_key(param):
        raise ValueError("分页参数格式不正确")
    detail = api.get("detail_path")
    if (not isinstance(detail, str) or not detail.startswith("/") or detail.startswith("//")
            or re.search(r"[\\\s?#]", unquote(detail))
            or any(part in (".", "..") for part in unquote(detail).split("/"))):
        raise ValueError("请输入同站详情路径，例如 /news/{id}")
    template = re.sub(r"\{(?:id|slug|category_name)\}", "", detail)
    if template == detail or "{" in template or "}" in template:
        raise ValueError("详情路径需包含 {id}、{slug} 或 {category_name}")
    params = urlencode([(key, value) for key, value in pairs if key != param])
    query = "&".join(filter(None, (urlencode({param: "{page}"}, safe="{}"), params)))
    return dict(site, api_list=f"{parts.path}?{query}", api_url="{base}" + detail, scan_mode="full")


def _api_samples(site, fetcher, result):
    chain = site["channel"] == "json-list"
    category = (site.get("api_categories") or [None])[0] if chain else None
    try:
        url = (json_list._cat_url(site["base"], site["api_list"], 1, category) if chain else
               json_api.page_url(site))
    except (KeyError, ValueError):
        result["issues"].append(_issue(site["base"], "invalid_api_configuration"))
        return
    result["source"] = url
    _, status, body = fetcher.get_many([url], timeout=12)[0]
    if status != 200:
        result["issues"].append(_issue(url, "fetch_failed", status))
        return
    payload = parse_json_tolerant(body)
    items = json_api.response_items(payload, site.get("api_items"))
    if items is None and chain and site.get("api_items"):
        items = json_api.response_items(payload, "data")
    if items is None or (chain and not all(isinstance(item, dict) for item in items)):
        result["issues"].append(_issue(url, "invalid_api_response", status))
        return
    for item in items[:SAMPLE_SIZE]:
        if not isinstance(item, dict):
            continue
        if chain:
            detail = json_list._detail_url(site, site["base"], site["api_url"], item, category)
            _, detail_status, detail_body = fetcher.get_many([detail], timeout=12)[0]
            sample, issue = _html_sample(detail, detail_status, detail_body, site, {})
            if issue:
                result["issues"].append(issue)
        elif "status" not in item or item["status"] == "published":
            sample = _sample(map_json_item(item, site), site)
        else:
            continue
        if sample:
            result["samples"].append(sample)
    if not result["samples"] and not result["issues"]:
        result["issues"].append(_issue(url, "no_article"))


def _preview(value, raw_rules, channel="auto", selector="", scope=None, api=None):
    mode, selected, path = _scope_options(scope)
    url = normalize_url(value)
    if not isinstance(channel, str) or channel not in HTML_CHANNELS | API_CHANNELS | {"wp-api", "auto", "archive"}:
        raise ValueError("请选择支持的采集通道")
    special = channel in API_CHANNELS | {"archive"}
    if special and (mode != "default" or selected or raw_rules):
        raise ValueError("此通道按入口采集，请使用默认范围")
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
    if special:
        parsed = urlsplit(url)
        site = {"domain": parsed.netloc, "base": f"{parsed.scheme}://{parsed.netloc}",
                "channel": channel, "via": "archive" if channel == "archive" else "direct",
                "_public_host": parsed.netloc, "_restricted_scope": True}
        if channel in API_CHANNELS:
            site = _api_site(site, api)
        site = job_site(site)
    else:
        try:
            site = job_site(resolve_site(url, check_entry=True, channel=channel))
        except PermissionError as exc:
            result["issues"].append({
                "url": url, "reason": "robots_blocked", "http_status": None,
                "detail": str(exc) or "robots.txt 禁止访问此路径",
                "hint": "可检查站点允许访问的路径，或查看可用的公开存档。",
            })
            return result
        except (ValueError, OSError, requests.RequestException) as exc:
            text = str(exc)
            issue = _issue(url, text)
            if any(code in text for code in ("HTTP 401", "HTTP 403", "HTTP 429")):
                issue["reason"] = "blocked"
                issue["hint"] = "可检查站点访问权限，或查看可用的公开存档。"
            result["issues"].append(issue)
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
    expected_transport = "archive" if channel == "archive" else "direct"
    if (channel not in HTML_CHANNELS | API_CHANNELS | {"wp-api", "archive"}
            or transport != expected_transport):
        result["issues"].append(_issue(entry, "preview_unavailable"))
        return result
    if rules and automatic and channel == "wp-api":
        return _preview(url, rules, "html", scope=scope)
    if rules and not result["editable"]:
        raise ValueError("此通道使用结构化字段，无法应用网页 XPath")
    if rules:
        site["schema"] = {**(site.get("schema") or {}), **rules}

    # The preview only requests public URLs on the resolved site. Its settings
    # never override transport, paging or concurrency in the full job.
    fetch_site = dict(site, _public_host=urlsplit(normalize_url(site["base"])).netloc)
    samples, issues = result["samples"], result["issues"]
    fetcher = Fetcher(fetch_site)
    if channel in API_CHANNELS:
        _api_samples(fetch_site, fetcher, result)
    elif channel == "wp-api":
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
        elif channel == "sitemap":
            urls = discover.sitemap_urls(sample_site, discovery, SAMPLE_SIZE, discovery_failures)
        else:
            urls, failures = archive.discover(site, sample_limit=discovery.remaining)
            if failures:
                discovery_failures.append({"url": entry, "error": "archive_index_unavailable", "status": 0})
            urls = [detail for detail in urls if urlsplit(detail).netloc == fetch_site["_public_host"]][
                :discovery.remaining]
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
        fallback = _preview(url, rules, "html", scope=scope)
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


def _capture_har(url):
    """实验性抓包：出口能起浏览器时抓一次 XHR/JSON，返回 HAR；否则 None。"""
    if not url:
        return None
    try:
        if not capture.available():
            return None
        wait_ms = int(os.getenv("CRAWL_PREVIEW_CAPTURE_WAIT_MS", "3000"))
        return capture.capture(url, wait_ms=wait_ms, public_host=urlsplit(url).netloc)
    except Exception:
        return None


def _homepage(url):
    """取一次首页 HTML 供接口挖掘；任何失败都返回空串，不影响预览主流程。"""
    if not url:
        return ""
    try:
        status, body = get_text(url, {"User-Agent": UA}, timeout=12,
                                public_host=urlsplit(url).netloc,
                                ignore_robots=not settings.respect_robots())
        return body if status == 200 else ""
    except Exception:
        return ""


class _ScriptFetcher:
    """给 endpoints.scan 的最小 fetcher：只拉同站脚本，单条失败不影响其它脚本。

    形状按 endpoints.scan 的约定：stream_many(urls) 产出 (url, status, body)；
    site["_public_host"] 让 scan 自己再过滤一次跨站脚本。
    """

    def __init__(self, url):
        netloc = urlsplit(url).netloc
        self.site = {"_public_host": netloc}
        self.netloc = netloc
        self.headers = {"User-Agent": UA}

    def stream_many(self, urls):
        for target in urls:
            if urlsplit(target).netloc != self.netloc:
                continue
            status, body = get_text(target, self.headers, timeout=6, public_host=self.netloc,
                                    ignore_robots=not settings.respect_robots())
            yield target, status, body if status == 200 else ""


def _html_endpoint_candidates(html, base_url):
    """首页 HTML → 接口候选。返回 (候选, 是否真的跑过扫描)。

    endpoints 是 T2 并行开发的模块：这里只做可选依赖，缺失或报错就跳过。
    传入极简 fetcher 是为了让内联脚本之外的 bundle 也能扫到（限 6 个、只限同站）。
    """
    if not html:
        return [], False
    try:
        from crawler import endpoints
    except Exception:
        return [], False
    try:
        found = endpoints.scan(html, base_url,
                              fetcher=_ScriptFetcher(base_url),
                              limit=6) or []
    except Exception:
        return [], False
    try:
        return contract.from_endpoints(found), True
    except Exception:
        return [], True


def _portable_query_key(name):
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return not (key in {"auth", "authorization", "key", "sig", "sign", "signature", "session",
                        "sessionid", "nonce", "password", "credential", "credentials"}
                or key.endswith(("apikey", "secret", "token", "signature")))


def _public_candidates(rows, url):
    host = urlsplit(url).netloc
    candidates = []
    for row in rows:
        if row["host"] != host or row["method"] != "GET":
            continue
        evidence = row["evidence"]
        sample = urlsplit(evidence["sample_url"])
        pairs = parse_qsl(sample.query, keep_blank_values=True)
        clean_pairs = [(name, value) for name, value in pairs if _portable_query_key(name)]
        if pairs != clean_pairs or sample.fragment:
            sample = sample._replace(query=urlencode(clean_pairs), fragment="")
        pagination = row["pagination"]
        reasons = row["reasons"]
        if pagination and not _portable_query_key(pagination["param"]):
            pagination = None
            reasons = [reason for reason in reasons if not reason.startswith("分页参数 ")]
        candidates.append(dict(row, query={k: v for k, v in row["query"].items() if _portable_query_key(k)},
                               pagination=pagination, reasons=reasons,
                               evidence=dict(evidence, sample_url=sample.geturl())))
    return candidates[:5]


def contract_entry(candidate, domain=None):
    """Render one candidate for copying into the sources list."""
    try:
        if not isinstance(candidate, dict):
            raise ValueError("缺少接口候选")
        if not candidate.get("host"):
            raise ValueError("候选缺少 host")
        host = candidate["host"]
        if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9.-]+(?::[0-9]+)?", host):
            raise ValueError("候选域名格式不正确")
        authority = urlsplit("https://" + host)
        if not authority.hostname or authority.port == 0:
            raise ValueError("候选域名格式不正确")
        if domain is not None and (not isinstance(domain, str) or domain.lower() != host.lower()):
            raise ValueError("候选接口须与站点域名一致")
        if candidate.get("method", "GET") != "GET" or candidate.get("channel") not in ("json-api", "json-list"):
            raise ValueError("仅支持生成公开 GET 接口的配置")
        path = candidate.get("path_template")
        if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
                or re.search(r"[\\?#\s]", path)):
            raise ValueError("候选接口路径格式不正确")
        query = candidate.get("query") or {}
        pagination = candidate.get("pagination") or {}
        if (not isinstance(query, dict) or not isinstance(pagination, dict)
                or not all(isinstance(values, list) and all(isinstance(value, str) for value in values)
                           for values in query.values())):
            raise ValueError("候选参数格式不正确")
        param = pagination.get("param")
        names = list(query)
        if param is not None:
            names.append(param)
        for name in names:
            if not isinstance(name, str) or not name or re.search(r"[&=;?#{}\s]", name):
                raise ValueError("候选参数格式不正确")
        clean_query = {k: v for k, v in query.items() if _portable_query_key(k)}
        export = dict(candidate, query=clean_query)
        if param and not _portable_query_key(param):
            export["pagination"] = None
        entry = contract.to_sources_entry(export, domain=domain)
        errors, warnings = validate_site(entry)
    except ValueError as exc:
        return {"entry": None, "errors": [str(exc)], "warnings": []}
    except (TypeError, AttributeError):
        return {"entry": None, "errors": ["候选配置格式不正确"], "warnings": []}
    if entry["channel"] == "json-list":
        warnings.append("详情地址由列表路径推断，使用前请核对。")
    if query != clean_query or export.get("pagination") != candidate.get("pagination"):
        warnings.append("配置已去掉请求中的凭证和临时参数。")
    result = {"entry": entry, "errors": errors, "warnings": warnings}
    if not errors:
        result["yaml"] = yaml.safe_dump([entry], allow_unicode=True, sort_keys=False)
    return result


def _with_contract(result, capture_contract=False, har=None):
    """按开关追加 candidates / capture_note；默认关闭时结果字典原样返回。

    来源优先级：显式传入的 HAR → capture_contract 抓包 → 首页 HTML 挖接口。
    capture_note 取值 har / html / unavailable。
    """
    if not capture_contract and har is None:
        return result
    result["candidates"] = []
    result["capture_note"] = "unavailable"
    base_url = result.get("url") or ""
    data = har
    if not data and capture_contract:
        data = _capture_har(base_url)
    if data:
        try:
            rows = contract.from_har(data)
        except Exception:
            rows = []
        if rows:
            result["candidates"] = rows[:5] if har else _public_candidates(rows, base_url)
            result["capture_note"] = "har"
            return result
        # 抓到了包但没有 xhr/JSON 证据：继续走首页 HTML 挖接口，来源按真实情况标

    found, scanned = _html_endpoint_candidates(_homepage(base_url), base_url)
    result["candidates"] = _public_candidates(found, base_url)
    result["capture_note"] = "html" if scanned else "unavailable"
    return result


def preview(value, raw_rules, channel="auto", selector="", scope=None,
            capture_contract=False, har=None, api=None):
    """工作台试采入口。

    capture_contract / har 是默认关闭的「抓包 → 候选契约」实验开关：
      - 都关闭时，返回值与行为与旧版完全一致（不多出任何键）；
      - har 给定：直接用这份 HAR 推断候选；
      - capture_contract=True：先抓一次包，抓不到再退回首页 HTML 挖接口。
    """
    result = _preview(value, raw_rules, channel, selector, scope, api)
    return _with_contract(result, capture_contract=capture_contract, har=har)


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
