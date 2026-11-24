"""Shared helpers."""
import html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlsplit

import requests, trafilatura
from lxml import html as lxml_html
from crawler.config import ROOT, DATA, DB, UA, COLS, MEDIA_COLS, WORKERS
from crawler.htmlpage import page_error


def sitemap_locs(body):
    """Parse sitemap XML without external entities; malformed/error pages are failures."""
    from lxml import etree
    try:
        # Some legacy generators put HTML named entities inside XML descriptions.
        # Convert only recognized names to numeric entities; keep structural parsing strict.
        from html.entities import name2codepoint
        body = re.sub(r"&([A-Za-z][A-Za-z0-9]+);", lambda m:
                      "&#%s;" % name2codepoint[m[1]] if m[1] in name2codepoint and
                      m[1] not in ("amp", "lt", "gt", "quot", "apos") else m[0], body)
        root = etree.fromstring(body.encode("utf-8"), etree.XMLParser(resolve_entities=False, no_network=True))
        kind = etree.QName(root).localname
        if kind not in ("sitemapindex", "urlset"):
            return None, []
        return kind, [str(v).strip() for v in root.xpath('//*[local-name()="loc"]/text()') if str(v).strip()]
    except (ValueError, TypeError, etree.XMLSyntaxError):
        return None, []

def wp_page_url(site, page, per=50, kind="posts"):
    per = int(site.get("wp_per_page") or per)
    """REST URL for one page of a collection; per-site ordering override supported."""
    ep = (site.get("wp_types") or {}).get(kind, f"wp/v2/{kind}")
    embed = "" if (kind == "media" or site.get("no_embed")) else "&_embed=1"
    order = site.get("orderby", "id")
    tail = f"&orderby={order}&order={site.get('order', 'asc')}" if order else ""
    # `_fields` 是为 posts 配的精简字段集，套到 media 上会把 source_url/mime_type 等滤掉，
    # 导致 media 行 url 为空、全部被丢弃；media 用独立字段表，未配置就不过滤。
    fields = site.get("wp_fields_media") if kind == "media" else site.get("wp_fields")
    if fields:
        tail += "&_fields=" + fields
    if site.get("api") == "rest_route":
        return f"{site['base'].rstrip('/')}/?rest_route=/{ep}&per_page={per}&page={page}{embed}{tail}"
    return f"{site['base'].rstrip('/')}/wp-json/{ep}?per_page={per}&page={page}{embed}{tail}"


def wp_type_inventory(site, fetcher):
    """Inspect REST types without enabling templates, forms or private endpoints."""
    base = site["base"].rstrip("/")
    url = base + ("/?rest_route=/wp/v2/types" if site.get("api") == "rest_route" else "/wp-json/wp/v2/types")
    from crawler.web import parse_json_tolerant
    _, status, body = next(iter(fetcher.get_many([url])))
    data = parse_json_tolerant(body) if status == 200 else None
    if not isinstance(data, dict) or "code" in data:
        return {"domain": site["domain"], "error": f"types unavailable (HTTP {status})", "types": []}
    rows = []
    for name, info in data.items():
        if not isinstance(info, dict) or not info.get("rest_base"):
            continue
        rows.append({"type": name, "route": f"{info.get('rest_namespace') or 'wp/v2'}/{info['rest_base']}",
                     "name": info.get("name"), "has_archive": info.get("has_archive"),
                     "configured": name in ("post", "page", "attachment") or name in (site.get("wp_types") or {}),
                     "needs_scope_review": name not in ("post", "page", "attachment") and name not in (site.get("wp_types") or {})})
    return {"domain": site["domain"], "types": rows}


def sitemap_urls(site, fetcher, limit, failures=None):
    return list(iter_sitemap_urls(site, fetcher, limit, failures=failures))


def declared_feed_urls(page_url, doc):
    """Resolve HTML feed declarations on the same site."""
    host = urlparse(page_url).netloc
    urls = []
    for link in doc.xpath("//head/link[@href and @type and @rel]"):
        mime = link.get("type").partition(";")[0].strip().lower()
        if "alternate" not in link.get("rel").lower().split() or mime not in {
            "application/rss+xml", "application/atom+xml", "application/rdf+xml"
        }:
            continue
        href = link.get("href").strip()
        if not href or re.search(r"\bcomments?\s+(?:rss|feed)\b", link.get("title", ""), re.I):
            continue
        try:
            url = urljoin(page_url, href)
            target = urlparse(url)
        except ValueError:
            continue
        if target.path.rstrip("/").endswith("/comments/feed"):
            continue
        if target.scheme in ("http", "https") and target.netloc == host:
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_feed(body, feed_url):
    """Return article links from RSS, RDF, or Atom. None means malformed or not a feed."""
    from lxml import etree
    if not isinstance(body, (str, bytes)) or not body:
        return None
    try:
        text_input = isinstance(body, str)
        root = etree.fromstring(body.encode("utf-8") if text_input else body,
                                etree.XMLParser(encoding="UTF-8" if text_input else None,
                                                resolve_entities=False, no_network=True))
    except (ValueError, etree.XMLSyntaxError):
        return None
    kind = etree.QName(root).localname.lower()
    if kind == "feed":
        links = root.xpath("./*[local-name()='entry']/*[local-name()='link'][@href and (not(@rel) or @rel='alternate')]")
    elif kind in ("rss", "rdf"):
        links = root.xpath("./*[local-name()='channel']/*[local-name()='item']/*[local-name()='link']"
                           " | ./*[local-name()='item']/*[local-name()='link']")
    else:
        return None
    urls = []
    for link in links:
        raw = (link.get("href") or link.text or "").strip()
        if not raw:
            continue
        try:
            url = urljoin(urljoin(feed_url, link.base or ""), raw)
            parsed = urlparse(url)
        except ValueError:
            continue
        if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
            urls.append(url)
    return list(dict.fromkeys(urls))


def in_path_scope(url, site):
    prefix = site.get("_path_scope")
    if not prefix:
        return True
    target = urlsplit(url)
    path = target.path or "/"
    return target.netloc == urlsplit(site["base"]).netloc and (
        prefix == "/" or path == prefix or path.startswith(prefix + "/")
    )


def rss_urls(site, fetcher, limit, failures=None):
    """RSS/Atom 条目 URL。feed 可带 {start} 或 {page} 占位符做翻页（配合 page_step/pages）。"""
    tpl = site.get("feed", "/feed/")
    count = int(site.get("pages", 1) or 1)
    if "{start}" in tpl:
        step = int(site.get("page_step", 10))
        feeds = [tpl.replace("{start}", str(i * step)) for i in range(count)]
    elif "{page}" in tpl:
        step = int(site.get("page_step", 1))
        feeds = [tpl.replace("{page}", str(1 + i * step)) for i in range(count)]
    else:
        feeds = [tpl]
    feeds = [urljoin(site["base"].rstrip("/") + "/", feed) for feed in feeds]
    out = []
    for u, status, body in fetcher.get_many(feeds):
        entries = parse_feed(body, u) if status == 200 else None
        if entries is None:
            if failures is not None:
                failures.append({"url": u, "status": status if status != 200 else 0})
            continue
        out.extend(entries)
    for u in site.get("seed_urls") or []:
        full = u if u.startswith("http") else site["base"].rstrip("/") + u
        if full not in out:
            out.append(full)
    out = list(dict.fromkeys(u for u in out if in_path_scope(u, site)))
    return out[:limit] if limit else out

def _page_urls(site):
    """列表页地址（支持 {page} / {offset} 占位符，page_step 为步长）。"""
    raw = site.get("list_urls") or [site.get("list_url") or (site["base"] + "/")]
    step = int(site.get("page_step", 10)); count = int(site.get("pages", 1))
    pages = []
    for u in raw:
        if u.startswith("/"):
            u = site["base"].rstrip("/") + u
        if "{offset}" in u:                       # e.g. /Actualites/(offset)/{offset}
            pages += [u.replace("{offset}", str(i * step)) for i in range(count)]
        elif "{page}" in u:                       # e.g. /actualites?page={page}
            steps = int(site.get("page_step", 1))
            pages += [u.replace("{page}", str(i * steps)) for i in range(count)]
        else:
            pages.append(u)
    return pages


ASSET = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|json|xml|pdf|zip|mp4|mp3|woff2?|ttf|eot)$", re.I)
SKIP_SCHEME = ("#", "javascript:", "mailto:", "tel:")


def html_urls(site, fetcher, limit, failures=None):
    """列表页 → 文章 URL。

    - `item_selector`：可选 XPath，返回 href 字符串（如 //a[contains(@href,'/news/')]/@href）；
    - `link_pattern`：对解析后的绝对 URL 过滤（既有正则都没有 ^ 锚点，语义不变）；
    - 相对链接用 urljoin 按所在列表页解析（旧实现用 base + "/" 拼，子目录站点会拼错）；
    - 资源/锚点/javascript 链接一律丢弃（旧实现会捡到 /media/ 下的 .css）。
    """
    pat = site.get("link_pattern")
    sel = site.get("item_selector")
    out, seen = [], set()
    pages = _page_urls(site)
    if getattr(fetcher, "ego", False) or getattr(fetcher, "browser", False) or getattr(fetcher, "cloud", False):
        results = fetcher.get_many(pages)
    else:
        results = list(fetcher.stream_many(pages, workers=4))
    host = urlparse(site["base"]).netloc
    for page_url, status, body in results:
        if status != 200 or not body or page_error(body):
            if failures is not None:
                failures.append({"url": page_url, "status": status if status != 200 else 0})
            continue
        try:
            doc = lxml_html.fromstring(body)
        except Exception:
            if failures is not None:
                failures.append({"url": page_url, "status": 0})
            continue
        hrefs = []
        if sel:                                   # 显式选择器：只信它，不再叠加启发式
            try:
                hrefs = [x for x in doc.xpath(sel) if isinstance(x, str)]
            except Exception:
                if failures is not None:
                    failures.append({"url": page_url, "status": 0, "error": "invalid_selector"})
                hrefs = []
        else:
            hrefs = [a.get("href") for a in doc.xpath("//a[@href]")]
        for href in hrefs:
            href = (href or "").strip()
            if not href or href.startswith(SKIP_SCHEME) or ASSET.search(href):
                continue
            full = urljoin(page_url, href)        # 先按列表页解析相对链接，再按绝对 URL 过滤
            if pat and not re.search(pat, full):
                continue
            if not pat and not sel and not re.search(r"/20\d\d/|/news/|/article|/story|/press|/media", full):
                continue
            if full not in seen and urlparse(full).netloc in (host, site["domain"]):
                seen.add(full); out.append(full)
    # 显式 seed：单页/静态页/无法从列表页发现的详情页（2026-11-12 全站详情页规则）
    for u in site.get("seed_urls") or []:
        full = u if u.startswith("http") else site["base"].rstrip("/") + u
        if full not in seen:
            seen.add(full); out.append(full)
    out = [url for url in out if in_path_scope(url, site)]
    return out[:limit] if limit else out


def iter_sitemap_urls(site, fetcher, cap=0, chunk_children=5, failures=None):
    """Yield sitemap URLs incrementally so a 400k-URL sitemap never sits in memory.

    failures: 可选 list；非 200 的 sitemap 入口（含子图）会 append {"url","status"}，
    由 channels/base 决定"可重试"还是"已删除"，避免一个子图 503 时整站被标 complete。
    """
    base = site["base"]
    sm = site.get("sitemap", "/sitemap.xml")
    entry = urljoin(base.rstrip("/") + "/", sm)
    seen, queue = {entry}, [entry]
    filt = site.get("url_filter")
    pat = site.get("link_pattern")
    yielded = 0
    while queue:
        batch, queue = queue[:chunk_children], queue[chunk_children:]
        for u, status, body in fetcher.get_many(batch):
            if status != 200:
                if failures is not None:
                    failures.append({"url": u, "status": status})
                continue
            xml_kind, locs = sitemap_locs(body)
            if xml_kind is None:
                if failures is not None:
                    failures.append({"url": u, "status": 0, "error": "invalid_sitemap"})
                continue
            locs = [urljoin(u, loc) for loc in locs]
            if xml_kind == "sitemapindex":
                queue += [l for l in dict.fromkeys(locs) if l not in seen]
                seen.update(locs)
                continue
            for l in locs:
                if re.search(r"\.(jpg|jpeg|png|gif|css|js|pdf|xml)$", l, re.I):
                    continue
                if filt and not re.search(filt, l):
                    continue
                if pat and not re.search(pat, l):
                    continue
                if not in_path_scope(l, site):
                    continue
                yield l
                yielded += 1
                if cap and yielded >= cap:
                    return
