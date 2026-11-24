#!/usr/bin/env python3
"""Probe the 109 target sites: reachability, platform, discovery channels, article extraction."""
import csv, json, re, ssl, sys, time, urllib.request, urllib.error, gzip, io
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

def fetch(url, timeout=25, maxbytes=900_000, ua=UA):
    req = urllib.request.Request(url, headers={
        "User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            raw = r.read(maxbytes)
            if r.headers.get("Content-Encoding") == "gzip":
                try: raw = gzip.decompress(raw)
                except Exception: pass
            return {"ok": True, "status": r.status, "url": r.geturl(), "headers": dict(r.headers),
                    "body": raw, "ms": int((time.time()-t0)*1000)}
    except urllib.error.HTTPError as e:
        try: raw = e.read(maxbytes)
        except Exception: raw = b""
        return {"ok": False, "status": e.code, "url": url, "headers": dict(e.headers or {}),
                "body": raw, "ms": int((time.time()-t0)*1000), "err": "http"}
    except Exception as e:
        return {"ok": False, "status": 0, "url": url, "headers": {}, "body": b"",
                "ms": int((time.time()-t0)*1000), "err": type(e).__name__ + ": " + str(e)[:120]}

def text(b):
    if isinstance(b, bytes):
        for enc in ("utf-8", "latin-1"):
            try: return b.decode(enc, "replace")
            except Exception: pass
    return b or ""

def strip_tags(h):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = re.sub(r"&nbsp;", " ", h)
    h = re.sub(r"&amp;", "&", h)
    return re.sub(r"\s+", " ", h).strip()

def origin(u):
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}"

def probe(row):
    dom = row["domain"]
    res = {"domain": dom, "name": row.get("name",""), "country": row.get("country",""),
           "type": row.get("type",""), "lang_listed": row.get("lang","")}
    # 1) home page
    r = fetch("https://" + dom + "/")
    if not r["ok"] and r["status"] == 0:
        r = fetch("http://" + dom + "/")
    body = text(r["body"])
    res.update({
        "home_status": r["status"], "home_final": r.get("url",""), "home_ms": r["ms"],
        "server": (r["headers"].get("Server") or "")[:40],
        "err": r.get("err",""),
        "size": len(r["body"]),
    })
    res["https_ok"] = bool(r["ok"] and str(r.get("url","")).startswith("https"))
    gen = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', body, re.I)
    res["generator"] = gen.group(1)[:60] if gen else ""
    res["title"] = (re.search(r"(?is)<title[^>]*>(.*?)</title>", body).group(1).strip()[:120]
                    if re.search(r"(?is)<title[^>]*>(.*?)</title>", body) else "")
    low = body.lower()
    res["wp_hint"] = ("wp-content" in low) or ("wp-json" in low) or ("wordpress" in low)
    res["cf"] = "cloudflare" in (r["headers"].get("Server","")+str(r["headers"])).lower()

    base = origin(r.get("url") or ("https://"+dom)) if r["status"] else "https://"+dom
    res["base"] = base

    # 2) WordPress REST
    wp = {"ok": False}
    if r["status"] and not r.get("err"):
        w = fetch(base + "/wp-json/wp/v2/posts?per_page=1")
        body_w = text(w["body"])[:2000]
        if w["ok"] and body_w.strip().startswith("["):
            try:
                arr = json.loads(text(w["body"]))
                wp = {"ok": True, "total": int(w["headers"].get("X-WP-Total") or 0),
                      "pages": int(w["headers"].get("X-WP-TotalPages") or 0),
                      "latest": arr[0]["date"] if arr else "", "api": base + "/wp-json/wp/v2/posts"}
            except Exception as e:
                wp = {"ok": False, "err": str(e)[:80]}
        else:
            wp = {"ok": False, "status": w["status"], "ct": (w["headers"].get("Content-Type") or "")[:40]}
    res["wp"] = wp

    # 3) feeds
    feeds = []
    feed_paths = ["/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/?feed=rss2", "/news/feed", "/feed.xml"]
    for p in feed_paths:
        u = base + p
        f = fetch(u, timeout=15, maxbytes=400_000)
        if not f["ok"]: continue
        b = text(f["body"])
        if "<rss" in b[:2000] or "<feed" in b[:2000] or "<rdf:RDF" in b[:500]:
            items = len(re.findall(r"<item[\s>]", b)) or len(re.findall(r"<entry[\s>]", b))
            links = re.findall(r"<link[^>]*>([^<]+)</link>", b) or re.findall(r'<link[^>]*href="([^"]+)"', b)
            latest = ""
            m = re.search(r"<pubDate>([^<]+)</pubDate>", b) or re.search(r"<updated>([^<]+)</updated>", b)
            if m: latest = m.group(1)
            ftitle = re.search(r"<title[^>]*>(.*?)</title>", b, re.S)
            feeds.append({"path": p, "items": items, "latest": latest.strip()[:32],
                          "title": strip_tags(ftitle.group(1))[:60] if ftitle else "",
                          "link_sample": (links[0] if links else "")[:100]})
        if len(feeds) >= 2: break
    res["feeds"] = feeds

    # 4) robots + sitemap
    rob = fetch(base + "/robots.txt", timeout=15, maxbytes=60_000)
    rb = text(rob["body"])[:4000] if rob["ok"] else ""
    res["robots"] = {"status": rob["status"], "sitemaps": re.findall(r"(?im)^sitemap:\s*(\S+)", rb)[:3],
                     "disallow": re.findall(r"(?im)^disallow:\s*(\S+)", rb)[:6]}
    sms = []
    for p in ["/sitemap.xml", "/wp-sitemap.xml", "/sitemap_index.xml"]:
        s = fetch(base + p, timeout=20, maxbytes=600_000)
        if not s["ok"]: continue
        b = text(s["body"])
        if "<urlset" in b[:2000] or "<sitemapindex" in b[:2000]:
            n = len(re.findall(r"<loc>", b))
            kind = "index" if "<sitemapindex" in b[:2000] else "urlset"
            sms.append({"path": p, "type": kind, "urls": n})
        if len(sms) >= 1: break
    res["sitemaps"] = sms

    # 5) article extraction test
    art = {"url": ""}
    target = ""
    for f in feeds:
        if f.get("link_sample", "").startswith("http"):
            target = f["link_sample"]; break
    if not target:
        # discover first plausible article link from home page
        hrefs = re.findall(r'href="(https?://[^"]+|/[^"#?]{8,})"', body)
        scored = [h for h in hrefs if re.search(r"(20\d\d|/news/|/article|/story|/post)", h) and not re.search(r"\.(jpg|png|css|js|pdf)$", h)]
        if scored: target = urljoin(base + "/", scored[0])
    if target:
        a = fetch(target, timeout=25, maxbytes=900_000)
        ab = text(a["body"])
        art_block = re.search(r"(?is)<article[^>]*>(.*?)</article>", ab)
        seg = art_block.group(1) if art_block else ab
        paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", seg)
        txt = strip_tags(" ".join(paras))
        og = re.search(r'property=["\']og:image["\'][^>]+content=["\']([^"\']+)', ab)
        pub = re.search(r'property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)', ab) or \
              re.search(r'<time[^>]+datetime=["\']([^"\']+)', ab)
        author = re.search(r'name=["\']author["\'][^>]+content=["\']([^"\']+)', ab)
        art = {"url": target[:120], "status": a["status"], "text_len": len(txt),
               "paras": len(paras), "og_image": bool(og), "date": (pub.group(1)[:25] if pub else ""),
               "author_meta": (author.group(1)[:40] if author else ""),
               "article_tag": bool(art_block)}
    res["article"] = art
    return res

def main():
    rows = list(csv.DictReader(open("research/sources.csv", newline="", encoding="utf-8")))
    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(probe, r): r for r in rows}
        for i, f in enumerate(as_completed(futs), 1):
            r = futs[f]
            try:
                out.append(f.result())
            except Exception as e:
                out.append({"domain": r["domain"], "error": repr(e)[:200]})
            print(f"[{i}/{len(rows)}] {r['domain']}", flush=True)
    out.sort(key=lambda x: x.get("domain",""))
    json.dump(out, open("research/probe_results.json","w"), ensure_ascii=False, indent=1)
    print("done", len(out))

if __name__ == "__main__":
    main()
