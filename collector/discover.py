#!/usr/bin/env python3
"""Find a discovery channel (sitemap / feed / listing links) for sites we have no data for.

  python3 collector/discover.py            # report only
  python3 collector/discover.py --apply    # write the found channel back to sources.yaml
"""
import argparse, json, os, re, sys
import requests, yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
SITEMAPS = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/news-sitemap.xml",
            "/sitemap-news.xml", "/post-sitemap.xml", "/sitemap-index.xml"]
FEEDS = ["/feed/", "/feed", "/rss", "/rss.xml", "/atom.xml", "/?feed=rss2", "/index.xml"]

def get(url, timeout=25):
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)[:80]

def looks_article(u):
    return bool(re.search(r"/20\d\d[/-]|/news/|/article|/story|/press|/media|/post/", u)) and not re.search(r"\.(jpg|png|gif|css|js|pdf)$", u, re.I)

def probe(site):
    base = site["base"].rstrip("/")
    rec = {"domain": site["domain"], "channel": None, "path": None, "urls": 0, "note": ""}
    for path in SITEMAPS:
        st, body = get(base + path)
        if st == 200 and ("<urlset" in body[:3000] or "<sitemapindex" in body[:3000]):
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
            if "<sitemapindex" in body[:3000] and locs:      # follow first child
                st2, child = get(locs[0])
                if st2 == 200: locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", child)
            arts = [l for l in locs if looks_article(l)]
            rec.update(channel="sitemap", path=path, urls=len(arts) or len(locs))
            return rec
    for path in FEEDS:
        st, body = get(base + path)
        if st == 200 and ("<rss" in body[:1500] or "<feed" in body[:1500] or "<rdf:RDF" in body[:600]):
            items = len(re.findall(r"<item[\s>]", body)) or len(re.findall(r"<entry[\s>]", body))
            rec.update(channel="rss", path=path, urls=items); return rec
    # listing fallback: count article-ish links on the homepage
    st, body = get(base + "/")
    if st == 200:
        links = {u for u in re.findall(r'href="([^"#]+)"', body) if looks_article(u)}
        rec.update(channel="html" if links else None, path=None, urls=len(links),
                   note="homepage links" if links else f"http {st}, no sitemap/feed/article links")
    else:
        rec["note"] = f"http {st}"
    return rec

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); a = ap.parse_args()
    sites = yaml.safe_load(open(os.path.join(ROOT, "sources.yaml")))
    def rows(d):
        p = os.path.join(DATA, d, "urls.txt")
        return sum(1 for _ in open(p)) if os.path.exists(p) else 0
    todo = [s for s in sites if s["channel"] == "html" and rows(s["domain"]) == 0]
    print(f"{len(todo)} html sites with no data")
    out = []
    for s in todo:
        r = probe(s)
        out.append(r)
        print(f"  {r['domain']:<34} {str(r['channel']):<8} {r['urls']:>5}  {r['note']}")
    json.dump(out, open(os.path.join(DATA, "discover.json"), "w"), ensure_ascii=False, indent=1)
    if a.apply:
        by = {r["domain"]: r for r in out}
        for s in sites:
            r = by.get(s["domain"])
            if not r or not r["channel"]: continue
            if r["channel"] in ("sitemap", "rss"):
                s["channel"] = r["channel"]
                s["sitemap" if r["channel"] == "sitemap" else "feed"] = r["path"]
            elif r["channel"] == "html" and r["urls"] >= 5:
                s["channel"] = "html"   # keep, but the generic listing rule will now find links
        yaml.safe_dump(sites, open(os.path.join(ROOT, "sources.yaml"), "w"), allow_unicode=True, sort_keys=False)
        print("sources.yaml updated")

if __name__ == "__main__":
    main()
