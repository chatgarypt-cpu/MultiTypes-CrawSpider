"""从页面与 JS bundle 里挖接口候选：纯 Python 可离线跑，jsluice 存在时增强。

被 `crawler/adhoc.py` 的 detect() 第 2.5 步调用：只往 `detected.endpoints` /
`detected.suggested` 附加信息，不改 detect() 的 channel（自动改通道只在
`ENDPOINT_AUTOCONFIG=1` 时生效）。

设计要点：
- `extract` 只认「调用点里的字符串」与「含 API 特征的路径字符串」，静态资源后缀、
  明显的页面路径直接丢，避免把 /about、/logo.png 当接口；
- 结果统一成 {kind, url, source, score, reasons}，score 只用于排序/阈值判断，
  不参与任何抓取语义；
- `scan` 只拉同站脚本：fetcher 带 `_public_host` 时顺手当白名单用，public 预览不会
  因为一个 CDN 脚本就去请求第三方域名。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

JSLUICE_TIMEOUT = 20          # 单个文件跑 jsluice 的上限，超时当没结果
SUGGEST_MIN = 50              # suggest() 的最低分，低于它只当线索不给配置片段

# 调用点：fetch("/x") / axios.get("/x") / $.getJSON("/x") / jQuery.ajax({url: "/x"})
FETCH_RE = re.compile(
    r"""(?:fetch|axios(?:\.\w+)?|\$\.(?:get|post|ajax|getJSON)|jQuery\.(?:get|post|ajax|getJSON))"""
    r"""\s*\(\s*(['"`])([^'"`\n]{2,400})\1""", re.I)
# XMLHttpRequest.open("GET", "/x")：第二个参数才是 URL
XHR_RE = re.compile(
    r"""\.open\s*\(\s*(['"`])(?:GET|POST|PUT|DELETE|PATCH|HEAD)\1\s*,\s*(['"`])([^'"`\n]{2,400})\2""", re.I)
# 普通字符串字面量：只有当路径带 API 特征时才收
STRING_RE = re.compile(r"""(['"`])([^'"`\n\\]{2,400})\1""")

API_HINT = re.compile(r"/(?:api|apis|ajax|rest|json|graphql|wp-json|v\d+)(?:[/?#._-]|$)", re.I)
ID_SEG = re.compile(r"^(?:\d+|\d{4}-\d{2}-\d{2}|[0-9a-f]{8,}|[0-9a-f-]{32,})$", re.I)
VERSION_SEG = re.compile(r"/v\d+/", re.I)
# 静态资源后缀：留着 .json —— 它经常就是接口本身（/_next/data、?format=json）
NOISE_EXT = re.compile(
    r"\.(?:js|mjs|css|map|png|jpe?g|gif|webp|svg|ico|bmp|woff2?|ttf|otf|eot|"
    r"mp4|webm|ogg|mp3|wav|pdf|zip|gz|tgz|tar|rar|7z|exe|dmg|apk|ipa|txt|xml|html?)(?:[?#]|$)",
    re.I)
NOISE_PATH = re.compile(r"/(?:static|assets|images?|img|fonts?|css|js|icons?|sprites?|locales?)/", re.I)

# 埋点/会话/心跳类路径：真实存在但不是内容接口，降分而不是丢（/api/session/list 这类仍要看）
NOISE_TAIL = re.compile(
    r"/(?:track|tracking|analytics|collect|beacon|ping|heartbeat|csrf|nonce|token|"
    r"session|sessions|geo|consent|optin|opt-out)(?:[/?#._-]|$)", re.I)
# 内容型接口（能直接拿来采集的）：WP REST 根/内容空间、版本化的内容路径
CONTENT_API = re.compile(r"/(?:wp-json/?$|wp-json/wp/v\d+|api/v\d+/(?:posts|pages|articles|news|stories|items))",
                         re.I)

KIND_BASE = {"api": 30, "graphql": 34, "next_data": 30, "rest_route": 28, "path": 10}
AUTO_KINDS = {"api", "next_data", "rest_route", "graphql"}


# ---------- 打分 / 分类 ----------

def _depth(path):
    return len([seg for seg in (path or "").split("/") if seg])


def _kind(url):
    low = url.lower()
    path = urlsplit(low).path
    if "rest_route=" in low:
        return "rest_route"
    if "graphql" in path:
        return "graphql"
    if "/_next/data/" in low:
        return "next_data"
    if "wp-json" in low or API_HINT.search(path):
        return "api"
    return "path"


def _score(url, kind, called, bonus=0):
    """分高只代表「更像接口」，不保证一定能用。"""
    low = url.lower()
    path = urlsplit(low).path
    score = KIND_BASE.get(kind, 10)
    if called:
        score += 18
    if API_HINT.search(path):
        score += 10
    if VERSION_SEG.search(path):
        score += 3
    if kind in ("graphql", "next_data", "rest_route"):
        score += 6
    if NOISE_TAIL.search(path):
        score -= 25                     # 埋点/心跳：留着给人看，但别让它抢到建议位
    if CONTENT_API.search(url):
        score += 8                      # wp-json 根 / wp/v2 / api/vN/posts 这类能直接采
    score += min(_depth(path), 3) * 2
    return max(0, min(100, score + bonus))


def _reasons(url, kind, called, extra=None):
    low = url.lower()
    path = urlsplit(low).path
    out = []
    if called:
        out.append("call")
    out.append(kind)
    if API_HINT.search(path):
        out.append("api-path")
    if VERSION_SEG.search(path):
        out.append("versioned")
    if "rest_route=" in low:
        out.append("rest_route")
    if NOISE_TAIL.search(path):
        out.append("noise")
    if CONTENT_API.search(url):
        out.append("content-api")
    if extra:
        out.extend(extra)
    return list(dict.fromkeys(out))


def _acceptable(url, called, kind):
    """过滤静态资源与明显无关路径；纯字符串必须带 API 特征才收。"""
    path = urlsplit(url).path
    if NOISE_EXT.search(path) or NOISE_PATH.search(path):
        return False
    if kind == "path" and not called:
        return False
    if not called and kind == "api" and not API_HINT.search(path) and "wp-json" not in url.lower():
        return False
    return True


def _same_site(url, base_url):
    """候选是否属于同一个站点（含子域/父域互认）；base 解析不出来就放行。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
        base = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return True
    if not host or not base:
        return True
    return host == base or host.endswith("." + base) or base.endswith("." + host)


def _absolute(literal, base_url):
    lit = (literal or "").strip()
    if not lit or lit.startswith(("#", "javascript:", "data:", "mailto:", "tel:", "blob:")):
        return ""
    if lit.startswith("?") and "rest_route=" not in lit:
        return ""                       # 纯查询串不是接口线索
    if not lit.startswith(("/", "./", "../", "//", "http://", "https://")) \
            and not (lit.startswith("?") and "rest_route=" in lit):
        return ""                       # MIME 类型、变量名之类的裸字符串不是路径
    if "{" in lit and "rest_route=" not in lit:
        if "/_next/data/" in lit:
            lit = re.sub(r"\$\{[^}]*\}", "__BUILD_ID__", lit)   # buildId 稍后由页面回填
        else:
            return ""                   # 其它未填充的模板字符串，绝对化没有意义
    url = urljoin(base_url or "", lit)
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


def _merge(items):
    """按 url 去重：保留最高分，reasons 合并。"""
    merged = {}
    for item in items:
        url = item.get("url")
        if not url:
            continue
        cur = merged.get(url)
        if cur is None:
            merged[url] = dict(item, reasons=list(item.get("reasons") or []))
            continue
        if int(item.get("score") or 0) > int(cur.get("score") or 0):
            cur["score"], cur["kind"] = item.get("score"), item.get("kind")
        for reason in item.get("reasons") or []:
            if reason not in cur["reasons"]:
                cur["reasons"].append(reason)
        if not cur.get("source") and item.get("source"):
            cur["source"] = item["source"]
    return list(merged.values())


# ---------- 抽取 / 扫描 ----------

def _literals(js_text):
    """返回 {字面量: called}；called=True 表示出现在 XHR 调用点里。"""
    found = {}
    for match in FETCH_RE.finditer(js_text):
        found.setdefault(match.group(2), True)
    for match in XHR_RE.finditer(js_text):
        found.setdefault(match.group(3), True)
    for match in STRING_RE.finditer(js_text):
        found.setdefault(match.group(2), False)
    return found


def extract(js_text, base_url, source=""):
    """从一段 JS 文本里抽 API 候选，返回 list[dict]。

    dict: {"kind": api|graphql|next_data|rest_route|path, "url", "source", "score", "reasons"}
    """
    if not isinstance(js_text, str) or not js_text:
        return []
    items = []
    for literal, called in _literals(js_text).items():
        url = _absolute(literal, base_url)
        if not url or not _same_site(url, base_url):
            continue
        kind = _kind(url)
        if not _acceptable(url, called, kind):
            continue
        items.append({"kind": kind, "url": url, "source": source,
                      "score": _score(url, kind, called), "reasons": _reasons(url, kind, called)})
    return _merge(items)


NEXT_BUILD_RE = re.compile(r'"buildId"\s*:\s*"([A-Za-z0-9_\-]{4,})"')
NEXT_STATIC_RE = re.compile(r"/_next/static/([A-Za-z0-9_\-]{4,})/")
WP_ROOT_RE = re.compile(r"^/wp-json/?$", re.I)
# 值得直接采的 WP 内容空间（按概率排序）；自定义类型留给人工加
WP_CONTENT_PATHS = ("wp/v2/posts", "wp/v2/pages", "wp/v2/media")


def _next_build_id(html_text):
    """从 __NEXT_DATA__ 或 /_next/static/<id>/ 里取 buildId；取不到返回空串。"""
    if not isinstance(html_text, str) or not html_text:
        return ""
    match = NEXT_BUILD_RE.search(html_text) or NEXT_STATIC_RE.search(html_text)
    return match.group(1) if match else ""


def _fold_build_id(url, build_id):
    """把 `/_next/data/${buildId}/x.json` 这类运行时占位替换成真实 buildId。"""
    if not build_id or "/_next/data/" not in url:
        return url
    parts = urlsplit(url)
    segments = parts.path.split("/")
    # ["", "_next", "data", "<占位或 id>", ...]
    if len(segments) > 3 and segments[1:3] == ["_next", "data"]:
        token = segments[3]
        if any(mark in token for mark in ("$", "{", "}", "%7B", "%7D", "__")):
            segments[3] = build_id
            return urlunsplit((parts.scheme, parts.netloc, "/".join(segments), parts.query, ""))
    return url


def _wp_routes_candidates(base_url, fetcher, roots):
    """WP REST 根 → 读 routes 目录 → 产出真正能采的内容接口（每个 95 分）。"""
    if not roots or fetcher is None:
        return []
    method = getattr(fetcher, "stream_many", None) or getattr(fetcher, "get_many", None)
    if method is None:
        return []
    body = ""
    try:
        for _url, status, text in method(roots[:1]):
            if status == 200 and isinstance(text, str):
                body = text
    except Exception:
        return []
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return []
    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(routes, dict):
        return []
    root = urljoin(base_url or "", "/wp-json/")
    out = []
    for path in WP_CONTENT_PATHS:
        if "/" + path in routes:
            out.append({"kind": "api", "url": root + path, "source": "wp-routes",
                        "score": 95, "reasons": ["wp-routes", "content-api"]})
    return out


def _public_host(fetcher):
    site = getattr(fetcher, "site", None)
    if isinstance(site, dict):
        return site.get("_public_host") or ""
    return ""


def _parse_scripts(html_text, base_url):
    """返回 (内联脚本文本, 外链脚本 URL)。lxml 解析失败就当成没有脚本。"""
    if isinstance(html_text, bytes):
        html_text = html_text.decode("utf-8", "replace")
    if not isinstance(html_text, str) or not html_text.strip():
        return [], []
    try:
        from lxml import html
        doc = html.fromstring(html_text)
        inline = [t for t in doc.xpath("//script[not(@src)]/text()") if t and t.strip()]
        srcs = []
        for src in doc.xpath("//script[@src]/@src"):
            url = urljoin(base_url or "", (src or "").strip())
            if url.startswith(("http://", "https://")) and url not in srcs:
                srcs.append(url)
        return inline, srcs
    except Exception:
        return [], []


def scan(html, base_url, fetcher=None, limit=12):
    """扫首页：内联脚本 + 外链脚本（有 fetcher 时拉取，最多 limit 个）。

    返回 {"candidates": [...], "scripts": n, "via": "python"|"jsluice"}。
    fetcher 只要提供 stream_many(urls) 形状 ((url, status, body)) 即可。
    """
    inline, srcs = _parse_scripts(html, base_url)
    host = _public_host(fetcher)
    if host:
        srcs = [u for u in srcs if urlsplit(u).netloc == host]     # public 预览不跨站
    fetched = []
    if srcs and fetcher is not None:
        batch = srcs[:max(1, int(limit or 12))]
        method = getattr(fetcher, "stream_many", None) or getattr(fetcher, "get_many", None)
        try:
            for url, status, body in (method(batch) if method else ()):
                if status == 200 and isinstance(body, str) and body.strip():
                    fetched.append((body, url))
        except Exception:
            pass                                   # 拉脚本失败不该影响页面挖接口

    texts = [(text, "inline") for text in inline] + fetched
    candidates = []
    for text, source in texts:
        candidates.extend(extract(text, base_url, source=source))

    via = "python"
    extra = _jsluice_scan([text for text, _ in texts], base_url)
    if extra:
        via = "jsluice"
        candidates.extend(extra)

    # Next.js：把 /_next/data/${buildId}/x.json 的占位换成页面里真实 buildId
    build_id = _next_build_id(html if isinstance(html, str) else "")
    if build_id:
        candidates = [dict(item, url=_fold_build_id(item["url"], build_id)) for item in candidates]

    # WP：出现 /wp-json 根就去读 routes 目录，直接给出能采的内容接口
    # 只要出现任何 /wp-json… 线索就去读一次根目录（站点常只引用插件接口，不写根路径）
    if any("/wp-json" in urlsplit(item["url"]).path for item in candidates):
        root_url = urljoin(base_url or "", "/wp-json/")
        candidates.extend(_wp_routes_candidates(base_url, fetcher, [root_url]))
    return {"candidates": _merge(candidates), "scripts": len(texts), "via": via,
            "build_id": build_id}


# ---------- jsluice（可选二进制） ----------

def _jsluice_available():
    return shutil.which("jsluice") is not None


def _run_jsluice(path):
    """跑 `jsluice urls <file>`，返回 jsonl 文本；非 0 退出当失败。"""
    result = subprocess.run(["jsluice", "urls", path], capture_output=True,
                            text=True, timeout=JSLUICE_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip()[:120] or f"exit {result.returncode}")
    return result.stdout or ""


def jsluice_extract(paths, base_url=""):
    """jsluice 存在时逐文件抽 URL 并合并；不存在返回 []。

    jsonl 每行一个对象，url/link/endpoint 任一字段都认，相对路径按 base_url 绝对化。
    """
    if not _jsluice_available():
        return []
    items = []
    for path in paths or []:
        try:
            text = _run_jsluice(path)
        except Exception:
            continue
        for line in (text or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            info = record.get("info") if isinstance(record.get("info"), dict) else {}
            raw = record.get("url") or record.get("link") or record.get("endpoint") or info.get("url")
            if not isinstance(raw, str) or not raw.strip():
                continue
            url = _absolute(raw, base_url)
            if not url:
                continue
            kind = _kind(url)
            if not _acceptable(url, True, kind):
                continue
            items.append({"kind": kind, "url": url, "source": "jsluice",
                          "score": _score(url, kind, True, bonus=8),
                          "reasons": _reasons(url, kind, True, ["jsluice"])})
    return _merge(items)


def _jsluice_scan(texts, base_url):
    """把脚本正文落到临时文件再让 jsluice 过一遍；任何异常都当没结果。"""
    if not texts or not _jsluice_available():
        return []
    try:
        with tempfile.TemporaryDirectory(prefix="endpoints-") as tmp:
            paths = []
            for index, text in enumerate(texts[:12]):
                path = os.path.join(tmp, f"bundle-{index}.js")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                paths.append(path)
            return jsluice_extract(paths, base_url=base_url)
    except Exception:
        return []


# ---------- 排序 / 建议配置 ----------

def rank(candidates):
    """score 降序；同分同 kind 时浅路径在前；再按 url 保证稳定。"""
    items = [c for c in candidates or [] if isinstance(c, dict) and c.get("url")]
    return sorted(items, key=lambda c: (-int(c.get("score") or 0), str(c.get("kind") or ""),
                                        _depth(urlsplit(c["url"]).path), c["url"]))


def _page_template(url):
    """生成 json-list 的 api_list 模板：只留 {page}，其余段保持原样。"""
    parts = urlsplit(url)
    segments = [seg for seg in parts.path.split("/") if seg]
    if segments and ID_SEG.match(segments[-1]):
        segments[-1] = "{page}"                     # 末尾 id 当翻页位
    path = "/" + "/".join(segments) if segments else "/"
    query = parse_qsl(parts.query, keep_blank_values=True)
    page_key = ""
    for key, value in query:
        if key.lower() in ("page", "p", "pn", "offset", "start", "from") and re.fullmatch(r"\d*", value or ""):
            page_key = key
            break
    if page_key:
        query = [(k, "{page}" if k == page_key else v) for k, v in query]
    else:
        query.append(("page", "{page}"))
    return urlunsplit(("", "", path, urlencode(query, safe="{}"), ""))


def _detail_shaped(url):
    """中间段是 id/uuid 的更像详情接口，不适合直接当列表模板。"""
    segments = [seg for seg in urlsplit(url).path.split("/") if seg]
    return any(ID_SEG.match(seg) for seg in segments[:-1])


def _index_like(url):
    """/wp-json、/api、/rest 这类是「接口目录」，不是内容列表，不能当 json-list 模板。"""
    path = (urlsplit(url).path or "").rstrip("/").lower()
    return path in ("", "/wp-json", "/rest", "/api", "/apis", "/json")   # /graphql 是接口本身，不算目录


def _suggest_for(kind, url, score):
    parts = urlsplit(url)
    if kind == "api" and ("/wp/v2/" in url or "rest_route=/wp/v2/" in url):
        api = "rest_route" if "rest_route=" in url else "wp-json"
        return {"channel": "wp-api", "api": api, "confidence": int(score), "evidence": url,
                "note": "WordPress REST 内容接口；wp_types 可按站点实际自定义类型补充"}
    if kind == "graphql":
        path = parts.path or "/graphql"
        if "{" in path:
            return None
        return {"channel": "json-api", "api": path, "api_query": "limit=100&meta=total_count",
                "confidence": int(score), "evidence": url}
    if _detail_shaped(url):
        return None
    if not CONTENT_API.search(url):
        # 插件/基础设施接口（newspack、woocommerce、cellmoni…）：能给 API 路径，但条目字段是猜的，
        # 不冒充 json-list 模板（api_list + api_items 猜错会让人直接采到一堆元数据）。
        return {"channel": "json-api", "api": parts.path or "/", "confidence": int(score),
                "evidence": url, "note": "插件/基础设施接口：api 可用，条目字段需人工确认"}
    return {"channel": "json-list", "api_list": _page_template(url), "api_items": "items",
            "api_url": "{base}/item/{id}", "confidence": int(score), "evidence": url,
            "note": "候选片段：api_items / api_url 需按站点实际字段调整"}


def suggest(candidates):
    """最强候选够格时给一份 sources.yaml 片段，否则 None。"""
    for item in rank(candidates):
        kind = item.get("kind")
        if kind not in AUTO_KINDS or int(item.get("score") or 0) < SUGGEST_MIN:
            continue
        if "noise" in (item.get("reasons") or []) or _index_like(item["url"]):
            continue                        # 埋点/心跳、接口目录：只在线索里展示，不给配置
        entry = _suggest_for(kind, item["url"], item["score"])
        if entry:
            return entry
    return None
