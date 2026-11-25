"""Shared helpers."""
import html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests, trafilatura
from crawler.text import clean, iframes, first_img, meta, iso, parse_dt
from crawler.config import ROOT, DATA, DB, UA, COLS, MEDIA_COLS, WORKERS

def map_wp_post(p, site):
    emb = p.get("_embedded", {}) or {}
    terms = [t for group in (emb.get("wp:term") or []) for t in group]
    cats = [t["name"] for t in terms if t.get("taxonomy") == "category"]
    tags = [t["name"] for t in terms if t.get("taxonomy") == "post_tag"]
    author = (emb.get("author") or [{}])[0]
    content = p.get("content", {}).get("rendered", "")
    yimg = ((p.get("yoast_head_json") or {}).get("og_image") or [{}])[0].get("url", "")
    frames = iframes(content)
    return {
        "title": clean(p.get("title", {}).get("rendered", "")), "sub_title": "", "url": p.get("link", ""),
        "channel_text": cats[0] if cats else "", "country_text": site.get("country", ""),
        "lang_text": site.get("lang", ""), "user_id": str(p.get("author") or ""),
        "user_author_name": author.get("name", ""), "user_url": author.get("link", ""),
        "created_at": iso(parse_dt(p.get("date_gmt"))),
        "description": clean(p.get("excerpt", {}).get("rendered", "")), "text": clean(content),
        "image": p.get("jetpack_featured_media_url") or yimg or first_img(content),
        "video": next((f for f in frames if re.search(r"youtube|vimeo|dailymotion", f)), ""),
        "link": next((f for f in frames if not re.search(r"youtube|vimeo|dailymotion", f)), ""),
        "tag": "; ".join(tags), "host": site["domain"],
        "updated_at": iso(parse_dt(p.get("modified_gmt"))),
    }

def map_wp_media(p, site):
    md = p.get("media_details") or {}
    return {
        "id": p.get("id", ""),
        "title": clean(p.get("title", {}).get("rendered", "")),
        "url": p.get("source_url", ""),
        "link": p.get("link", ""),
        "mime": p.get("mime_type", ""),
        "width": md.get("width", ""),
        "height": md.get("height", ""),
        "alt": p.get("alt_text", ""),
        "caption": clean(p.get("caption", {}).get("rendered", "")),
        "description": clean(p.get("description", {}).get("rendered", "")),
        "created_at": iso(parse_dt(p.get("date_gmt"))),
        "parent": p.get("post") or "",
        "host": site["domain"],
    }


def _flatten_rich(node, out=None):
    """Collect plain text from a Directus/Quill style rich-text tree (inner: str | list | dict)."""
    if out is None: out = []
    if node is None:
        return out
    if isinstance(node, str):
        if node.strip(): out.append(node.strip())
    elif isinstance(node, dict):
        inner = node.get("inner")
        if isinstance(inner, (str, dict)): _flatten_rich(inner, out)
        elif isinstance(inner, list): _flatten_rich(inner, out)
    elif isinstance(node, list):
        for v in node: _flatten_rich(v, out)
    return out


def map_json_item(it, site, prefix_map=None):
    parts = [_flatten_rich(it.get("content"))]
    for sec in (it.get("sections") or []):
        if isinstance(sec, dict):
            props = sec.get("props") or {}
            if props.get("content"): parts.append(_flatten_rich(props["content"]))
    text = " ".join(x for p in parts for x in p)
    if not text and it.get("description"): text = str(it["description"])
    slug = it.get("slug") or str(it.get("id"))
    pre = ""
    if prefix_map and it.get("category") is not None:
        try: pre = prefix_map(it["category"])
        except Exception: pre = ""
    url = f"{site['base'].rstrip('/')}/blog/" + (f"{pre}/" if pre else "") + slug
    return {
        "title": (it.get("title") or "").strip(),
        "url": url,
        "text": text,
        "description": (it.get("description") or "")[:500] if isinstance(it.get("description"), str) else "",
        "created_at": it.get("date_created") or "",
        "author": "", "image": "", "tag": "",
    }
