"""HTML 页面流水线：剪枝降噪（→ Markdown）→ 结构化提取（→ 31 列行）。

对应通用爬虫三层模型里的后两层，全部复用已装依赖（trafilatura / lxml），不引入新包：

- 剪枝与降噪层：trafilatura 抽取正文并输出 Markdown；用「链接文字占比 + 正文长度」
  判断列表页/导航页（这类页面以前会被当成文章写入），判定为列表页的行直接丢弃。
- 结构化提取层：sources.yaml 里的 `schema:`（字段 → XPath）做确定性的字段提取；
  `llm_fields:` 在配置了 LLM_API_KEY 时用任意 OpenAI 兼容接口补空字段（可选，失败静默跳过）。

抓取/渲染层不在这里：继续用 crawler.web.Fetcher（direct / curl_cffi / ego / cloud）。
"""
import json
import os
import re

import trafilatura
from lxml import html as lxml_html

# 允许 LLM 补的字段（31 列里的自由文本字段；数值/URL 字段不让模型猜）
LLM_ALLOWED = {"title", "created_at", "user_author_name", "channel_text", "description",
               "tag", "lang_text", "sub_title"}


# ---------- 剪枝与降噪 ----------
def markdown(body, url=""):
    """正文 → 干净 Markdown；失败返回空串（调用方回退到纯文本候选）。"""
    if not body:
        return ""
    try:
        return trafilatura.extract(body, url=url, output_format="markdown",
                                   include_formatting=True, include_links=False,
                                   include_comments=False, include_tables=True) or ""
    except Exception:
        return ""


def _doc(body):
    try:
        doc = lxml_html.fromstring(body)
    except Exception:
        return None
    for bad in doc.xpath("//script | //style | //noscript | //template"):
        bad.drop_tree()
    return doc


def _norm(text):
    return " ".join((text or "").split())


def link_ratio(body):
    """可见文字里落在 <a> 里的比例。列表页 / 导航页接近 1，文章页通常 < 0.3。"""
    doc = _doc(body)
    if doc is None:
        return 0.0
    total = len(_norm(doc.text_content()))
    if total == 0:
        return 0.0
    linked = sum(len(_norm(a.text_content())) for a in doc.xpath("//a"))
    return min(1.0, linked / total)


GATE_RATIO = 0.35          # 默认链接密度阈值：文字超过 35% 在 <a> 里 → 列表/导航页
GATE_MD_MAX = 1200         # Markdown 长于此值一律当正文（避免误杀长文章）


def page_error(body):
    """Identify access/error documents before any article or metadata fallback."""
    title = re.search(r"<title[^>]*>(.*?)</title>", body or "", re.I | re.S)
    title = _norm(title.group(1)).lower() if title else ""
    if any(marker in title for marker in (
            "just a moment", "one moment", "robot challenge", "checking your browser",
            "attention required!", "access denied", "security check", "请稍候")):
        return "challenge"
    if any(marker in title for marker in (
            "404 not found", "page not found", "service unavailable", "account suspended",
            "site under construction", "internal server error")):
        return "error_page"
    if "cf-chl-" in (body or "") or "sgcaptcha" in (body or "").lower():
        return "challenge"
    return ""


def gate_setting(site):
    """(是否启用闸门, 阈值)。`noise_gate` 支持 bool 或数字，数字即按站覆盖链接密度阈值。

    - 缺省：html 通道默认启用，其它通道默认关闭；
    - true/false：显式开关，用默认阈值 0.35；
    - 0.65 这类数字：启用并改用该阈值（有些站点文章页导航链接多，默认 0.35 会误杀，
      例如正文页和导航页的链接密度可能存在明显差异）。"""
    gate = site.get("noise_gate")
    if gate is None:
        return site.get("channel") == "html", GATE_RATIO
    if gate is True:
        return True, GATE_RATIO
    if gate is False:
        return False, GATE_RATIO
    try:
        return True, float(gate)
    except (TypeError, ValueError):
        return site.get("channel") == "html", GATE_RATIO


def looks_like_listing(body, md="", ratio=GATE_RATIO):
    """列表页/导航页判定：正文短且文字主要在链接里。文章页不放行误杀（md 足够长就放过）。"""
    if len(md or "") >= GATE_MD_MAX:
        return False
    return link_ratio(body) > ratio


# ---------- 结构化提取：XPath schema ----------
def xpath_value(body, expr):
    """XPath → 第一个非空结果（属性/文本/元素文本均可）。expr 形如 //h1 或 //meta/@content。"""
    doc = _doc(body)
    if doc is None or not expr:
        return ""
    try:
        found = doc.xpath(str(expr))
    except Exception:
        return ""
    if not isinstance(found, list):
        return _norm(str(found))
    for item in found:
        value = _norm(item.text_content()) if hasattr(item, "text_content") else _norm(str(item))
        if value:
            return value
    return ""


def apply_schema(row, body, site):
    """按 sources.yaml 的 `schema:` 覆盖字段。只覆盖非空值，绝不写空串。"""
    schema = (site or {}).get("schema") or {}
    for field, expr in schema.items():
        value = xpath_value(body, expr)
        if value:
            row[field] = value
    return row


# ---------- 结构化提取：可选 LLM ----------
def llm_config():
    """(key, base_url, model) 或 None。支持 LLM_* / OPENAI_API_KEY / DEEPSEEK_API_KEY。"""
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    deepseek = bool(os.environ.get("DEEPSEEK_API_KEY")) and not os.environ.get("LLM_BASE_URL")
    base = os.environ.get("LLM_BASE_URL") or ("https://api.deepseek.com" if deepseek else "https://api.openai.com/v1")
    model = os.environ.get("LLM_MODEL") or ("deepseek-chat" if "deepseek" in base else "gpt-4o-mini")
    return key, base.rstrip("/"), model


def _post_json(url, payload, headers, timeout):
    """单独一层，测试里直接换掉；httpx 缺失时视为不可用。"""
    import httpx
    return httpx.post(url, json=payload, headers=headers, timeout=timeout)


def llm_extract(md, fields, timeout=45):
    """Markdown + 字段清单 → dict。未配置 key / 网络失败 / JSON 坏掉都返回 {}。"""
    cfg = llm_config()
    if not cfg or not md or not fields:
        return {}
    key, base, model = cfg
    prompt = ("从下面的网页 Markdown 中提取字段，只输出一个 JSON 对象，键名固定为："
              + ", ".join(sorted(fields))
              + "。找不到的字段省略，不要编造。不要输出解释或代码块。\n\n" + md[:12000])
    payload = {"model": model, "temperature": 0,
               "messages": [{"role": "user", "content": prompt}],
               "response_format": {"type": "json_object"}}   # DeepSeek/Ollama 都只保证 json_object
    try:
        resp = _post_json(base + "/chat/completions", payload,
                          {"Authorization": "Bearer " + key, "Content-Type": "application/json"}, timeout)
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text or "", flags=re.I)
        data = json.loads(text)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(v).strip() for k, v in data.items()
            if k in fields and isinstance(v, (str, int, float)) and str(v).strip()}


def llm_enrich(row, md, site):
    """只补空字段（llm_override: true 才覆盖），字段必须在 31 列白名单里。"""
    fields = [f for f in ((site or {}).get("llm_fields") or []) if f in LLM_ALLOWED]
    if not fields:
        return row
    data = llm_extract(md or row.get("text", ""), fields)
    override = bool(site.get("llm_override"))
    for field, value in data.items():
        if override or not str(row.get(field) or "").strip():
            row[field] = value
    return row


# ---------- 主入口 ----------
def enrich(row, body, url, site):
    """抽取到手的一行 → 过闸门 + schema + 可选 LLM。被判定为列表页时返回 None。"""
    site = site or {}
    gate, ratio = gate_setting(site)          # html 默认开；noise_gate 可 false/数字
    md = markdown(body, url) if (gate or site.get("llm_fields")) else ""
    if gate and looks_like_listing(body, md, ratio):
        return None
    if site.get("schema"):
        apply_schema(row, body, site)
    if site.get("llm_fields"):
        llm_enrich(row, md, site)
    return row
