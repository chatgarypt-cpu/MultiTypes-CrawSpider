"""抓包 → 接口契约：把 HAR 或 endpoints 扫描结果聚成可写进 sources.yaml 的候选。

只依赖标准库。目标是回答工作台上最常问的那句：「这条 XHR/JSON 请求能不能配成
json-api / json-list？」——给出路径模板、query 参数、分页推定、字段完整度和一个
用于排序的分数，剩下的交给人工确认（api_url 这类只能猜的字段会显式标注）。

约定：
- HAR 按 1.2 结构读：log.entries[].request{method,url,headers}、
  response{status,headers,content{mimeType,text}}；也接受裸 entry 列表。
- 只把 xhr/fetch 响应或 JSON 响应当候选（HTML 文档页面走 html 通道，不在这里）。
- 数字 / UUID / 长 hash 的路径段统一换成 {id}，这样翻页、详情页才会落到同一个模板上。
"""
import json
import re
from urllib.parse import parse_qsl, quote, urlsplit

XHR_TYPES = {"xhr", "fetch"}
# 数值/UUID/长 hash 段：当成 id 占位，否则同一接口会被拆成 N 个候选
NUMERIC = re.compile(r"^-?\d+$")
UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
LONG_HASH = re.compile(r"^(?:[0-9a-fA-F]{16,}|[A-Za-z0-9_-]{24,})$")
# 每个 query 参数最多留几个样例值（够推出分页/布尔开关，又不至于把候选撑爆）
QUERY_SAMPLES = 5
# 「最像数组」的键名，用于 api_items 的猜测
ITEM_HINTS = ("items", "data", "list", "results", "rows", "articles", "posts",
              "entries", "records", "docs", "stories", "nodes", "hits")
# endpoints.scan 的 kind → 建议通道
KIND_CHANNELS = {
    "api": "json-api", "json": "json-api", "json-api": "json-api", "rest": "json-api",
    "graphql": "json-api", "xhr": "json-api", "fetch": "json-api", "form": "html",
    "json-list": "json-list", "list": "json-list", "pagination": "json-list",
    "sitemap": "sitemap", "rss": "rss", "feed": "rss", "html": "html",
}


def _har_entries(har):
    """取出 entries；容忍 {log:{entries}} 和裸列表两种写法。"""
    if isinstance(har, dict):
        log = har.get("log")
        entries = log.get("entries") if isinstance(log, dict) else None
        if entries is None:
            entries = har.get("entries")
        return [entry for entry in entries or () if isinstance(entry, dict)]
    if isinstance(har, (list, tuple)):
        return [entry for entry in har if isinstance(entry, dict)]
    return []


def _host_allowed(host, allowed):
    """hosts=None 表示全收；否则按主机名或其子域匹配。"""
    if not allowed:
        return True
    host = host.lower()
    return any(host == item or host.endswith("." + item) for item in allowed)


def _segment_template(segment):
    """单段路径模板化：1 / 12 / UUID / 长 hash → {id}，扩展名保留。"""
    base, dot, ext = segment.partition(".")
    if not base:
        return segment
    if NUMERIC.match(base) or UUID.match(base) or LONG_HASH.match(base):
        return "{id}" + (dot + ext if dot else "")
    return segment


def path_template(path):
    """把路径里的 id 段换成 {id}；空路径按 / 处理。"""
    path = path or "/"
    return "/".join(_segment_template(segment) for segment in path.split("/"))


def _query_values(query_string):
    """query → {参数名: [样例值...]}，保持首次出现顺序并去重。"""
    values = {}
    for name, value in parse_qsl(query_string or "", keep_blank_values=True):
        bucket = values.setdefault(name, [])
        if value not in bucket and len(bucket) < QUERY_SAMPLES:
            bucket.append(value)
    return values


def _entry_sample(entry, allowed_hosts, min_bytes):
    """一条 HAR entry → 候选样本；不像接口响应就返回 None。"""
    request = entry.get("request") or {}
    url = request.get("url")
    if not isinstance(url, str) or not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = parts.netloc.lower()
    if not _host_allowed(host, allowed_hosts):
        return None
    method = str(request.get("method") or "GET").upper()
    response = entry.get("response") or {}
    content = response.get("content") or {}
    mime = str(content.get("mimeType") or "").split(";")[0].strip().lower()
    text = content.get("text")
    text = text.strip() if isinstance(text, str) else ""
    resource = str(entry.get("_resourceType") or "").lower()
    json_like = "json" in mime or text.startswith(("{", "["))
    if resource not in XHR_TYPES and not json_like:
        return None
    size = content.get("size") if isinstance(content.get("size"), int) else 0
    size = max(size, len(text.encode("utf-8", "ignore")))
    if not text or size < int(min_bytes or 0):
        return None
    try:
        status = int(response.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    return {
        "url": url,
        "host": host,
        "scheme": parts.scheme,
        "method": method,
        "path_template": path_template(parts.path),
        "query": _query_values(parts.query),
        "status": status,
        "started": str(entry.get("startedDateTime") or ""),
        "json": json_like,
        "text": text,
    }


def _type_name(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return "str"


def _walk_fields(data):
    """产出 (字段名, 值)：顶层键 + 一层嵌套（对象 a.b，数组 items[].k）。"""
    if isinstance(data, dict):
        for key, value in data.items():
            name = str(key)
            yield name, value
            if isinstance(value, dict):
                for sub, sub_value in value.items():
                    yield f"{name}.{sub}", sub_value
            elif isinstance(value, list):
                for element in value[:1]:
                    if isinstance(element, dict):
                        for sub, sub_value in element.items():
                            yield f"{name}[].{sub}", sub_value
    elif isinstance(data, list):
        for element in data[:1]:
            if isinstance(element, dict):
                for sub, sub_value in element.items():
                    yield f"[].{sub}", sub_value


def _infer_fields(texts):
    """字段推断：键 → {type, completeness}；完整度 = 出现该键的样本占比。"""
    seen, types, parsed = {}, {}, 0
    for text in texts:
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            continue
        parsed += 1
        for name, value in _walk_fields(data):
            seen[name] = seen.get(name, 0) + 1
            types.setdefault(name, set()).add(_type_name(value))
    if not parsed:
        return {}
    fields = {}
    for name, count in seen.items():
        kinds = types.get(name) or {"null"}
        if len(kinds) > 1:
            kinds = kinds - {"null"} or kinds
        fields[name] = {"type": sorted(kinds)[0], "completeness": round(count / parsed, 2)}
    return fields


def _infer_pagination(query):
    """分页推断：同一模板下仅一个数值型 query 变化（≥2 个不同取值）才算分页。"""
    varying = {}
    for name, values in (query or {}).items():
        if not values or not all(str(value).lstrip("-").isdigit() for value in values):
            continue
        numbers = sorted({int(value) for value in values})
        if len(numbers) >= 2:
            varying[name] = numbers
    if len(varying) != 1:
        return None
    param, values = next(iter(varying.items()))
    confidence = 0.55 + 0.15 * (len(values) - 1)
    if all(later - earlier == 1 for earlier, later in zip(values, values[1:])):
        confidence += 0.10
    return {"param": param, "values": values, "confidence": round(min(confidence, 0.95), 2)}


def _list_field(fields):
    """fields 里最像「列表」的键：类型是 list，且名字命中常见命名。"""
    best = None
    for name, meta in (fields or {}).items():
        if not isinstance(meta, dict) or meta.get("type") != "list":
            continue
        base = name.rsplit(".", 1)[-1].replace("[]", "").lower()
        weight = (2 if base in ITEM_HINTS else 0, meta.get("completeness") or 0, -len(name))
        if best is None or weight > best[0]:
            best = (weight, name)
    return best[1] if best else None


def _score(sample_count, json_ratio, pagination, fields, items_key, status_codes):
    """打分：多条命中 + JSON + 分页 + 字段数（0~100，只用于排序）。"""
    score = 5.0 * min(sample_count, 5)
    reasons = [f"命中 {sample_count} 条响应"]
    if json_ratio >= 0.99:
        score += 15
        reasons.append("响应均为 JSON")
    elif json_ratio >= 0.5:
        score += 8
        reasons.append("响应多为 JSON")
    if pagination:
        score += 20
        arrow = "→".join(str(value) for value in pagination["values"])
        reasons.append(f"分页参数 {pagination['param']}（{arrow}）")
    if fields:
        score += 1.5 * min(len(fields), 10)
        reasons.append(f"字段 {len(fields)} 个")
    if items_key:
        score += 5
        reasons.append(f"含列表字段 {items_key}")
    if 200 in status_codes:
        score += 5
        reasons.append("HTTP 200")
    return round(min(score, 100.0), 1), reasons


def _candidate(key, items):
    """一组同模板样本 → 一条候选契约。"""
    host, method, template = key
    query = {}
    for item in items:
        for name, values in item["query"].items():
            bucket = query.setdefault(name, [])
            for value in values:
                if value not in bucket and len(bucket) < QUERY_SAMPLES:
                    bucket.append(value)
    pagination = _infer_pagination(query)
    fields = _infer_fields([item["text"] for item in items])
    items_key = _list_field(fields)
    status_codes = sorted({item["status"] for item in items if item["status"]})
    times = [item["started"] for item in items if item["started"]]
    json_ratio = sum(1 for item in items if item["json"]) / len(items)
    score, reasons = _score(len(items), json_ratio, pagination, fields, items_key, status_codes)
    return {
        "endpoint": f"{method} {host}{template}",
        "channel": "json-list" if pagination else "json-api",
        "method": method,
        "host": host,
        "path_template": template,
        "query": query,
        "pagination": pagination,
        "fields": fields,
        "sample_count": len(items),
        "score": score,
        "reasons": reasons,
        "evidence": {"time_range": [times[0], times[-1]] if times else [],
                     "status_codes": status_codes,
                     "sample_url": items[0]["url"]},
    }


def from_har(har, hosts=None, min_bytes=200):
    """HAR → 候选契约列表（按分数降序）。

    hosts：只收这些主机（含子域）；None = 全收。min_bytes：响应体至少这么大才算证据。
    """
    if isinstance(hosts, str):
        hosts = [hosts]
    allowed = {str(host).strip().lower() for host in hosts or () if str(host).strip()} or None
    groups, order = {}, []
    for entry in _har_entries(har):
        sample = _entry_sample(entry, allowed, min_bytes)
        if sample is None:
            continue
        key = (sample["host"], sample["method"], sample["path_template"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(sample)
    candidates = [_candidate(key, groups[key]) for key in order]
    candidates.sort(key=lambda item: (-item["score"], item["endpoint"]))
    return candidates


def _channel_for_kind(kind):
    kind = str(kind or "").strip().lower()
    if kind in KIND_CHANNELS:
        return KIND_CHANNELS[kind]
    if "list" in kind:
        return "json-list"
    return "json-api"


def from_endpoints(candidates):
    """endpoints.scan 的输出（kind/url/score）→ 与 from_har 同构的候选（无字段信息）。"""
    if isinstance(candidates, dict):
        # 容忍 {candidates: [...]} / {endpoints: [...]} 这类包装
        candidates = candidates.get("candidates") or candidates.get("endpoints") or []
    merged = {}
    order = []
    for item in candidates or ():
        if not isinstance(item, dict):
            continue
        raw = item.get("url") or item.get("endpoint") or ""
        if not isinstance(raw, str) or not raw.strip():
            continue
        raw = raw.strip()
        if raw.startswith("//"):
            raw = "https:" + raw
        if "://" not in raw:
            # 相对路径没有主机，无法独立成模板
            continue
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        method = str(item.get("method") or "GET").upper()
        key = (parts.netloc.lower(), method, path_template(parts.path))
        try:
            number = float(item.get("score") or 0)
        except (TypeError, ValueError):
            number = 0.0
        if 0 < number <= 1:
            number *= 100
        # 没有响应体证据，分数上限压到 70，避免盖过真正抓到 JSON 的候选
        score = round(min(70.0, 30.0 + min(100.0, max(0.0, number)) * 0.4), 1)
        kind = str(item.get("kind") or "").strip().lower()
        if key in merged:
            merged[key]["sample_count"] += 1
            merged[key]["score"] = max(merged[key]["score"], score)
            continue
        order.append(key)
        merged[key] = {
            "endpoint": f"{method} {key[0]}{key[2]}",
            "channel": _channel_for_kind(kind),
            "method": method,
            "host": key[0],
            "path_template": key[2],
            "query": _query_values(parts.query),
            "pagination": None,
            "fields": {},
            "sample_count": 1,
            "score": score,
            "reasons": [f"来自 endpoints.scan（kind={kind or 'unknown'}）",
                        "仅有 URL 形状，无响应体证据"],
            "evidence": {"time_range": [], "status_codes": [], "sample_url": raw},
        }
    rows = [merged[key] for key in order]
    rows.sort(key=lambda item: (-item["score"], item["endpoint"]))
    return rows


def to_sources_entry(candidate, domain=None):
    """候选 → sources.yaml 片段（json-api 或 json-list）。

    - json-list 的 api_list 用 path_template + {page} 占位（分页参数名来自抓包）。
    - api_url 只能猜：用同样的路径模板 + {id}，并在 note 里标注需要人工确认。
    """
    if not isinstance(candidate, dict):
        raise ValueError("候选必须是字典")
    host = str(candidate.get("host") or "").strip().lower()
    if not host:
        raise ValueError("候选缺少 host")
    evidence = candidate.get("evidence") or {}
    scheme = str(evidence.get("sample_url") or "").split("://")[0].lower()
    if scheme not in ("http", "https"):
        scheme = "https"
    template = str(candidate.get("path_template") or "/")
    pagination = candidate.get("pagination") or {}
    page_param = pagination.get("param")
    channel = candidate.get("channel") or ("json-list" if pagination else "json-api")
    params = [(str(name), list(values)) for name, values in (candidate.get("query") or {}).items()]
    entry = {"domain": domain or host, "channel": channel,
             "base": f"{scheme}://{host}", "via": "direct"}
    if channel == "json-list":
        pairs = [f"{page_param or 'page'}={{page}}"]
        for name, values in params:
            if name == page_param or not values:
                continue
            pairs.append(f"{name}={quote(str(values[0]))}")
        entry["api_list"] = template + "?" + "&".join(pairs)
        entry["api_url"] = "{base}" + (template if "{id}" in template else template.rstrip("/") + "/{id}")
        entry["note"] = "api_list 由抓包路径模板与分页参数生成；api_url 是猜测的详情模板，需人工确认"
    else:
        entry["api"] = template
        query = "&".join(f"{name}={quote(str(values[0]))}"
                         for name, values in params if values and name != page_param)
        if query:
            entry["api_query"] = query
        items_key = _list_field(candidate.get("fields") or {})
        if items_key:
            entry["api_items"] = items_key
    return entry


def _cell(value):
    """Markdown 单元格：竖线转义、去掉换行。"""
    return str(value).replace("|", "\\|").replace("\n", " ")


def render(candidates):
    """候选契约 → 人看的 Markdown 表。"""
    rows = [item for item in candidates or () if isinstance(item, dict)]
    if not rows:
        return "（没有可用的接口候选：抓包里没有 XHR/JSON 响应，也没从 HTML 扫到接口 URL）"
    lines = ["| # | 接口 | 分页 | 字段 | 命中 | 分数 | 建议通道 |",
             "|---|------|------|------|------|------|----------|"]
    for index, candidate in enumerate(rows, 1):
        pagination = candidate.get("pagination") or {}
        if pagination:
            values = "→".join(str(value) for value in (pagination.get("values") or [])[:4])
            page_cell = f"{pagination.get('param')} {values}（{pagination.get('confidence')}）"
        else:
            page_cell = "未发现"
        names = [f"{name}({meta.get('type')})"
                 for name, meta in (candidate.get("fields") or {}).items() if isinstance(meta, dict)]
        field_cell = "、".join(names[:4]) + (f" 等 {len(names)} 个" if len(names) > 4 else "")
        endpoint = candidate.get("endpoint") or candidate.get("path_template") or ""
        lines.append("| " + " | ".join([
            str(index), _cell(endpoint), _cell(page_cell), _cell(field_cell or "-"),
            str(candidate.get("sample_count") or 0), str(candidate.get("score") or 0),
            _cell(candidate.get("channel") or "-"),
        ]) + " |")
    return "\n".join(lines)
