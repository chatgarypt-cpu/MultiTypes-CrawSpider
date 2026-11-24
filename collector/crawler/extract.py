"""Shared helpers."""
import html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests, trafilatura
from crawler import htmlpage
from crawler.text import clean, iframes, first_img, meta, iso, parse_dt
from crawler.config import ROOT, DATA, DB, UA, COLS, MEDIA_COLS, WORKERS

def jsonld_blocks(body):
    """Every JSON-LD object on the page, flattened."""
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', body or "", re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                out.append(node)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return out

def ld_value(nodes, key):
    for n in nodes:
        v = n.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for k in ("name", "url", "@id"):
                if isinstance(v.get(k), str) and v[k].strip():
                    return v[k].strip()
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    return item["name"].strip()
    return ""

def extract_article(url, body, site):
    txt = trafilatura.extract(body, include_comments=False, include_tables=True,
                             prune_xpath=htmlpage.body_pruning(site)) or ""
    md = trafilatura.extract_metadata(body)
    ld = jsonld_blocks(body)

    title = (md.title if md and md.title else meta(body, r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                                                   r"<title[^>]*>(.*?)</title>"))
    date = (md.date if md and md.date else meta(body, r'property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
                                                  r'<time[^>]+datetime=["\']([^"\']+)', r'"datePublished"\s*:\s*"([^"]+)"'))
    updated = meta(body, r'property=["\']article:modified_time["\'][^>]+content=["\']([^"\']+)',
                        r'property=["\']og:updated_time["\'][^>]+content=["\']([^"\']+)') or ld_value(ld, "dateModified")
    dt = parse_dt(date)
    created = iso(dt) if dt else (date or "")
    dt_up = parse_dt(updated) if updated else None
    author = (md.author if md and md.author else "") or ld_value(ld, "author") or meta(body, r'name=["\']author["\'][^>]+content=["\']([^"\']+)')
    author_url = ""
    for n in ld:
        a = n.get("author")
        if isinstance(a, dict) and isinstance(a.get("url"), str):
            author_url = a["url"]; break
    section = (meta(body, r'property=["\']article:section["\'][^>]+content=["\']([^"\']+)')
               or ld_value(ld, "articleSection")
               or meta(body, r'name=["\']section["\'][^>]+content=["\']([^"\']+)'))
    if not section:                         # only a real category segment, never the article slug
        parts = [p for p in re.sub(r"^https?://[^/]+", "", url).split("/") if p and not re.fullmatch(r"\d{4}|\d{2}", p)]
        section = parts[0] if len(parts) >= 2 else ""
    tags = [html.unescape(t) for t in re.findall(r'property=["\']article:tag["\'][^>]+content=["\']([^"\']+)', body, re.I)]
    if not tags:
        kw = meta(body, r'name=["\']keywords["\'][^>]+content=["\']([^"\']+)')
        tags = [t.strip() for t in kw.split(",") if t.strip()]
    if not tags:
        kwld = ld_value(ld, "keywords")
        tags = [t.strip() for t in kwld.split(",") if t.strip()]
    lang = meta(body, r'<html[^>]+lang=["\']([^"\']+)') or site.get("lang", "")

    frames = iframes(body)
    return {
        "title": clean(title), "sub_title": "", "url": url,
        "channel_text": clean(section), "country_text": site.get("country", ""),
        "lang_text": lang,
        "user_id": "", "user_author_name": clean(author), "user_url": author_url,
        "created_at": created, "description": clean(md.description if md and md.description else
                        meta(body, r'property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                             r'name=["\']description["\'][^>]+content=["\']([^"\']+)')),
        "text": txt,
        "image": meta(body, r'property=["\']og:image["\'][^>]+content=["\']([^"\']+)') or first_img(body),
        "video": next((f for f in frames if re.search(r"youtube|vimeo|dailymotion", f)), ""),
        "link": next((f for f in frames if not re.search(r"youtube|vimeo|dailymotion", f)), ""),
        "tag": "; ".join(dict.fromkeys(tags)),
        "host": site["domain"],
        "updated_at": iso(dt_up) if dt_up else "",
    }


# ---------- multi-extractor candidates + scoring (news-please style) ----------

def _para_text(body, site=None):
    """Paragraph fallback after removing navigation; length must not reward menus."""
    doc = htmlpage._doc(body)
    if doc is None:
        return ""
    for node in doc.xpath(" | ".join(["//header", *htmlpage.body_pruning(site)])):
        if hasattr(node, "drop_tree") and node.getparent() is not None:
            node.drop_tree()
    roots = doc.xpath("//article | //main") or [doc]
    lines = [htmlpage._norm(p.text_content()) for root in roots
             for p in root.xpath(".//p | .//blockquote")]
    lines = list(dict.fromkeys(line for line in lines if len(line) >= 40))
    return "\n".join(lines)


def candidates(url, body, site=None):
    """Return [{extractor, text, title, date, score}] for one page."""
    out = []
    base = extract_article(url, body, site or {"base": url, "domain": ""})
    if base.get("text"):
        out.append({"extractor": "trafilatura", "text": base["text"], "title": base.get("title", ""),
                    "date": base.get("created_at", ""), "row": base})
    nodes = jsonld_blocks(body)
    if nodes:
        ld_text = ld_value(nodes, "articleBody") or ld_value(nodes, "description")
        if ld_text and len(ld_text) > 200:
            row = dict(base)
            if len(ld_text) > len(base.get("text", "")):
                row["text"] = clean(ld_text)
            row["title"] = row.get("title") or ld_value(nodes, "headline")
            row["created_at"] = row.get("created_at") or ld_value(nodes, "datePublished")
            out.append({"extractor": "jsonld", "text": row["text"], "title": row["title"],
                        "date": row["created_at"], "row": row})
    # 粗抽（整页可见文字）只做兜底：它一定比剪枝结果长（含导航/页脚），按长度择优会永远赢，
    # 于是 2026-11 之前入库的 html/rss 站正文常带 "Skip to main content / EXPLORE" 这类菜单。
    # 只有剪枝结果明显不合格（不足粗抽的 40%）时才让粗抽参与竞争。
    para = _para_text(body, site)
    base_text = base.get("text") or ""
    if len(para) > 200 and len(base_text) < 0.4 * len(para):
        row = dict(base)
        row["text"] = para
        out.append({"extractor": "html_text", "text": para, "title": row.get("title", ""),
                    "date": row.get("created_at", ""), "row": row})
    for c in out:
        c["score"] = _score(c)
    return sorted(out, key=lambda c: -c["score"])


def _score(c):
    text = c.get("text") or ""
    score = min(len(text), 6000) / 10.0
    score += 30 if c.get("title") else 0
    score += 20 if c.get("date") else 0
    score += 15 if text.count("\n") >= 3 else 0
    return score


def extract_best(url, body, site=None, min_text=60):
    """Pick the best candidate; return None when nothing reaches min_text.

    HTML 通道还会过列表页闸门、按 site 的 schema/llm_fields 补字段（crawler.htmlpage）。
    """
    if htmlpage.page_error(body) or htmlpage.is_navigation_page(url, body, site):
        return None
    cands = candidates(url, body, site)
    if not cands:
        return None
    best = cands[0]
    row = dict(best["row"])
    if len(row.get("text") or "") < min_text:
        return None
    row["text"] = best["text"]
    row["_extractor"] = best["extractor"]
    return htmlpage.enrich(row, body, url, site)


def extract_metadata(url, body, site=None):
    """正文抽不出来时的元数据兜底（2026-11-12 规则：短正文/无正文也保留条目）。

    优先复用候选行的 title/date/description；候选为空时退回 og/title/description。
    只返回有标题或描述的行，避免把错误页当详情页入库。
    """
    if htmlpage.page_error(body) or htmlpage.is_navigation_page(url, body, site):
        return None
    gate, ratio = htmlpage.gate_setting(site or {})
    if gate and htmlpage.looks_like_listing(body, htmlpage.markdown(body, url, site), ratio):
        return None
    cands = candidates(url, body, site)
    if cands:
        row = dict(cands[0]["row"])
        if not row.get("url"):
            row["url"] = url
        if row.get("title") or row.get("description"):
            row["_extractor"] = (cands[0].get("extractor") or "") + "+meta"
            return row
    title = clean(meta(body,
                       r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)',
                       r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']*)',
                       r"<title[^>]*>([^<]*)</title>"))
    desc = clean(meta(body,
                      r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)',
                      r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)'))
    if not title and not desc:
        return None
    date = meta(body, r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']*)')
    return {"title": title, "url": url, "description": desc, "text": "",
            "created_at": iso(parse_dt(date)), "host": (site or {}).get("domain", "")}
