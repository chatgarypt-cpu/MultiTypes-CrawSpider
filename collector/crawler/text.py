"""Shared helpers."""
import html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests, trafilatura
from crawler.config import ROOT, DATA, DB, UA, COLS, MEDIA_COLS, WORKERS

# ---------- text helpers ----------
def clean(h):
    if not h: return ""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|h\d|li|blockquote)>", "\n\n", h)
    h = re.sub(r"(?s)<[^>]+>", "", h)
    h = html.unescape(h)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t\r]+", " ", h)).strip()

def iframes(h):
    return re.findall(r'<iframe[^>]+src=["\']([^"\']+)', h or "", re.I)

def first_img(h):
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', h or "", re.I)
    return m.group(1) if m else ""

def meta(html_text, *patterns):
    for p in patterns:
        m = re.search(p, html_text or "", re.I)
        if m: return html.unescape(m.group(1))
    return ""

def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""

def parse_dt(value):
    """WP occasionally returns junk dates (year -1); never let one row kill a run."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None
