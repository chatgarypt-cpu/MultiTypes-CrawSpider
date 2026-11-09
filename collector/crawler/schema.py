"""Config schema for sources.yaml: catch typos before a crawl starts.

Each channel declares the keys it needs and the types it accepts. Unknown keys are
reported as warnings (they are usually typos of a known key), missing required keys
as errors. `python3 collector/collect.py --validate` checks the whole file.

Also detects duplicate mapping keys: PyYAML silently keeps the last value, which is
how a duplicated `list_urls` / `link_pattern` disappeared without any warning. We
walk the composed node tree ourselves and report every duplicate as an error.
"""
import re

import yaml

# required keys per channel
REQUIRED = {
    "wp-api": ["base"],
    "sitemap": ["base", "sitemap"],
    "rss": ["base", "feed"],
    "html": ["base"],
    "json-api": ["base", "api"],
    "json-list": ["base", "api_list", "api_url"],
}

# every key a channel may carry (plus the universally shared ones)
SHARED = {"domain", "name", "country", "lang", "type", "channel", "base", "via", "sitemap", "feed",
          "url_filter", "link_pattern", "min_text", "note", "notes", "detected", "no_embed",
          "pages", "page_step", "discover_cap", "orderby", "order", "via_posts", "via_pages", "via_media", "wp_per_page", "wp_fields",
          # html 页面流水线（crawler.htmlpage）：schema=XPath→字段，llm_fields=让可选 LLM 补的空字段
          "schema", "llm_fields", "llm_override", "noise_gate",
          # robots: false = 该站不按 robots.txt 限制（用户显式决定）；ego_wait_ms = 浏览器加载后等待
          "robots", "ego_wait_ms",
          # 人工确认不可采（无列表入口/站点下线/反爬）：不算配置错误，单独归类
          "disabled", "disabled_reason"}
OPTIONAL = {
    "wp-api": {"api"},
    "sitemap": set(),
    "rss": set(),
    "html": {"list_url", "list_urls", "item_selector"},
    "json-api": {"api_query", "api_page_size", "api_category", "api_items"},
    "json-list": {"api_categories", "api_items", "free_field", "premium_marker"},
}
VIA = {"direct", "impersonate", "cloud", "browser", "ego"}
TYPES = {
    "list_urls": list, "api_categories": list, "pages": int, "page_step": int,
    "api_page_size": int, "no_embed": bool, "min_text": int,
    "schema": dict, "llm_fields": list, "llm_override": bool,
    "item_selector": str, "robots": bool, "ego_wait_ms": int,
    "disabled": bool, "disabled_reason": str,
}
REGEX_KEYS = ("link_pattern", "url_filter")


def find_duplicate_keys(text, name="<yaml>"):
    """Return error strings for every duplicate mapping key in the YAML text.

    Uses `yaml.compose` so duplicate keys are visible before SafeLoader's dict
    construction silently overwrites the first value.
    """
    errors = []
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as e:
        return [f"{name}: YAML syntax error: {e}"]
    if root is None:
        return []

    def walk(node, context):
        if isinstance(node, yaml.MappingNode):
            local = context
            for key_node, value_node in node.value:
                if getattr(key_node, "value", None) == "domain" and isinstance(value_node, yaml.ScalarNode):
                    local = str(value_node.value)
            seen = {}
            for key_node, value_node in node.value:
                key = str(getattr(key_node, "value", key_node))
                line = key_node.start_mark.line + 1
                if key in seen:
                    where = f"{local}: " if local else ""
                    errors.append(f"{name}: {where}duplicate key {key!r} at line {line} (first at line {seen[key]})")
                else:
                    seen[key] = line
                walk(value_node, local)
        elif isinstance(node, yaml.SequenceNode):
            for child in node.value:
                walk(child, context)

    walk(root, "")
    return errors


def validate_site(site):
    """Return (errors, warnings) for one site dict."""
    errors, warnings = [], []
    if not isinstance(site, dict):
        return ["not a mapping"], []
    domain = site.get("domain") or "<missing domain>"
    channel = site.get("channel")
    if not channel:
        return [f"{domain}: missing channel"], []
    if channel not in REQUIRED:
        return [f"{domain}: unknown channel {channel!r}"], []
    for key in REQUIRED[channel]:
        if not site.get(key):
            errors.append(f"{domain}: channel {channel} requires {key!r}")
    if site.get("via") and site["via"] not in VIA:
        errors.append(f"{domain}: via must be one of {sorted(VIA)}, got {site['via']!r}")
    allowed = SHARED | OPTIONAL.get(channel, set()) | set(REQUIRED[channel])
    for key in site:
        if key not in allowed:
            warnings.append(f"{domain}: unknown key {key!r} (typo?)")
        typ = TYPES.get(key)
        if typ and key in site and not isinstance(site[key], typ):
            errors.append(f"{domain}: {key} must be {typ.__name__}")
    # noise_gate 允许 bool 或数字（数字=按站覆盖链接密度阈值，见 crawler/htmlpage.py）
    ng = site.get("noise_gate")
    if ng is not None and not isinstance(ng, bool) and not isinstance(ng, (int, float)):
        errors.append(f"{domain}: noise_gate must be bool or number, got {type(ng).__name__}")
    # 发现入口完整性：宁可在 validate 阶段吵，也不要抓了半天才发现没入口。
    # disabled 是显式的人工分类（站点下线/无列表入口/反爬），不算配置错误；
    # html 站没有 list_url/list_urls 又没 disabled，一律当 error 处理。
    disabled = site.get("disabled") is True
    if disabled:
        reason = site.get("disabled_reason") or "未写原因"
        missing_entry = channel == "html" and not (site.get("list_url") or site.get("list_urls"))
        category = "缺列表入口" if missing_entry else "不可访问/不采"
        warnings.append(f"{domain}: disabled site ({category}) — {reason}")
    elif channel == "html" and not (site.get("list_url") or site.get("list_urls")):
        errors.append(f"{domain}: html channel without list_url/list_urls "
                      f"(discovery entry missing; disable the site with disabled/disabled_reason if it is not crawlable)")
    if not disabled and site.get("disabled_reason"):
        warnings.append(f"{domain}: disabled_reason is set but disabled is not true")
    if channel == "sitemap" and not site.get("sitemap"):
        warnings.append(f"{domain}: sitemap channel without sitemap path (discovery entry missing)")
    # 正则 mismatch：写错的正则会让发现阶段静默 0 条
    for key in REGEX_KEYS:
        value = site.get(key)
        if isinstance(value, str) and value:
            try:
                re.compile(value)
            except re.error as e:
                warnings.append(f"{domain}: {key} is not a valid regex ({e}) -> discovery matches nothing")
        if value and channel not in ("html", "sitemap"):
            warnings.append(f"{domain}: {key} is ignored by channel {channel}")
    return errors, warnings


def validate_all(sites):
    errors, warnings = [], []
    seen = {}
    for s in sites or []:
        e, w = validate_site(s)
        errors += e; warnings += w
        d = s.get("domain") if isinstance(s, dict) else None
        if d in seen:
            warnings.append(f"{d}: duplicate entry")
        seen[d] = True
    return errors, warnings


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def validate_file(path):
    """Validate a sources.yaml file: duplicate keys + per-site schema.

    Returns (errors, warnings, sites). Duplicate keys are errors even though
    PyYAML loaded the file happily (it kept only the last value).
    """
    text = _read(path)
    errors = find_duplicate_keys(text, str(path))
    try:
        sites = yaml.safe_load(text) or []
    except yaml.YAMLError as e:
        return errors + [f"{path}: YAML syntax error: {e}"], [], []
    if not isinstance(sites, list):
        return errors + [f"{path}: expected a list of sites, got {type(sites).__name__}"], [], []
    e, w = validate_all(sites)
    return errors + e, w, sites
