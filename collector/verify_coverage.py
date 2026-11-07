#!/usr/bin/env python3
"""Coverage verification: compare what is on disk with an independent source total.

Per-channel strategy:
  wp-api          -> X-WP-Total via curl_cffi impersonation (works even when plain requests is 403)
  sitemap         -> <loc> count of the sitemap (index children followed)
  json-api        -> meta.total_count / aggregate
  json-list       -> last non-empty page probe (stories array)
  rss / html      -> try a sitemap first; otherwise mark unverifiable

  python3 collector/verify_coverage.py [--json]
"""
import argparse, json, os, re, sys
import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}


def rows(domain, kind="posts"):
    p = os.path.join(DATA, domain, "urls.txt" if kind == "posts" else f"{kind}.urls.txt")
    return sum(1 for _ in open(p)) if os.path.exists(p) else 0


def http(url, timeout=45, impersonate=None):
    if impersonate:
        from curl_cffi import requests as cffi
        return cffi.get(url, impersonate=impersonate, timeout=timeout, headers=UA)
    import requests
    return requests.get(url, timeout=timeout, headers=UA)


def wp_totals(site):
    """X-WP-Total for posts/pages/media; falls back to curl_cffi impersonation."""
    out = {}
    imp = "chrome" if site.get("via") in ("impersonate", "browser", "cloud") else None
    for kind in ("posts", "pages", "media"):
        path = f"/?rest_route=/wp/v2/{kind}&per_page=1" if site.get("api") == "rest_route" else f"/wp-json/wp/v2/{kind}?per_page=1"
        try:
            for attempt in (imp, "chrome"):
                r = http(site["base"].rstrip("/") + path, impersonate=attempt)
                if r.status_code == 200:
                    out[kind] = int(r.headers.get("X-WP-Total") or 0) or None
                    break
        except Exception:
            out[kind] = None
    return out


def sitemap_urls(site, limit_children=25):
    """Return the list of URLs advertised by the site sitemap."""
    base = site["base"].rstrip("/")
    path = site.get("sitemap") or "/sitemap.xml"
    if not path.startswith("http"):
        path = base + path
    seen, queue, urls = set(), [path], []
    while queue and len(seen) < limit_children:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            r = http(u)
            if r.status_code != 200:
                continue
            body = r.text
        except Exception:
            continue
        if "<sitemapindex" in body[:3000]:
            queue.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body))
        else:
            urls.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body))
    filt = site.get("url_filter")
    pat = site.get("link_pattern")
    if filt:
        urls = [u for u in urls if re.search(filt, u)]
    if pat:
        urls = [u for u in urls if re.search(pat, u)]
    if not filt and not pat:                      # only article-shaped URLs count for coverage
        urls = [u for u in urls
                if re.search(r"/20\d\d/|/20\d\d-|/news/|/article|/story|/post|/blog|/press", u)]
    return list(dict.fromkeys(urls))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    sites = []
    for s in yaml.safe_load(open(os.path.join(ROOT, "sources.yaml"))):
        if s["domain"] not in {x["domain"] for x in sites}:
            sites.append(s)
    report = []
    for s in sites:
        d = s["domain"]
        got = {k: rows(d, k) for k in ("posts", "pages", "media")}
        if sum(got.values()) == 0:
            continue
        rec = {"domain": d, "channel": s["channel"], "got": got,
               "expected": None, "source": None, "coverage": None}
        try:
            if s["channel"] == "wp-api":
                t = wp_totals(s)
                rec["expected"] = t
                rec["source"] = "X-WP-Total"
                pairs = [(got[k], t.get(k)) for k in ("posts", "pages", "media") if t.get(k)]
                rec["per_kind"] = {k: round(got[k] / t[k], 3) for k in ("posts", "pages", "media") if t.get(k)}
                rec["coverage"] = round(min(g / e for g, e in pairs), 3) if pairs else None
            elif s["channel"] == "sitemap":
                urls = sitemap_urls(s)
                rec["expected"] = len(urls) or None
                rec["source"] = "sitemap <loc>"
                rec["coverage"] = round(got["posts"] / len(urls), 3) if urls else None
            elif s["channel"] == "json-api":
                r = http(s["base"].rstrip("/") + s["api"] + "?limit=1&meta=total_count")
                rec["expected"] = (r.json().get("meta") or {}).get("total_count")
                rec["source"] = "Directus meta"
                rec["coverage"] = round(got["posts"] / rec["expected"], 3) if rec["expected"] else None
            else:
                urls = [u for u in sitemap_urls(s)
                        if re.search(r"/20\d\d/|/20\d\d-|/news/|/article|/story|/post|/blog|/press", u)]
                if urls:
                    rec["expected"] = len(urls)
                    rec["source"] = "sitemap <loc> (probed)"
                    rec["coverage"] = round(got["posts"] / len(urls), 3)
                else:
                    rec["source"] = "unverifiable"
        except Exception as e:
            rec["source"] = f"error: {str(e)[:40]}"
        report.append(rec)

    verified = [r for r in report if r["coverage"] is not None]
    complete = [r for r in verified if r["coverage"] >= 0.95]
    if a.json:
        print(json.dumps({"report": report, "verified": len(verified), "complete": len(complete)}, ensure_ascii=False, indent=1))
    else:
        print(f"{'domain':<34}{'channel':<10}{'got':>8}{'expected':>10}{'cov':>7}  per-kind")
        for r in sorted(report, key=lambda x: (x["coverage"] is None, x["coverage"] or 0)):
            exp = r["expected"]
            if isinstance(exp, dict):
                exp_s = str(sum(v for v in exp.values() if v))
            else:
                exp_s = str(exp) if exp else "-"
            cov = f"{r['coverage']:.0%}" if r["coverage"] is not None else "-"
            pk = r.get("per_kind")
            pk_s = " ".join(f"{k}{v:.0%}" for k, v in pk.items()) if pk else r["source"]
            print(f"{r['domain']:<34}{r['channel']:<10}{sum(r['got'].values()):>8}{exp_s:>10}{cov:>7}  {pk_s}")
        print(f"\n可验证 {len(verified)} 个，其中采全(>=95%) {len(complete)} 个，不可验证 {len(report)-len(verified)} 个")
    json.dump(report, open(os.path.join(DATA, "coverage_report.json"), "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
