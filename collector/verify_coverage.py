#!/usr/bin/env python3
"""Coverage verification: compare what is on disk with an independent source total.

Per-channel strategy (same-scope comparisons only):
  wp-api   -> X-WP-Total per kind (posts/pages/media) + sampled missing post URLs
  sitemap  -> <loc> set of the sitemap (index children followed) + exact missing-URL diff
  json-api -> meta.total_count (upstream only exposes a total, no URL list)
  rss/html -> try a sitemap first; otherwise mark unverifiable

Coverage guard: a local/upstream ratio is only reported when the upstream total is a
plausible independent count for the *same scope*. If expected is missing / 0 / clearly
smaller than local (expected <= local / 1.5, i.e. coverage >= 1.5), coverage is set to
None, verdict becomes "口径异常", and the record is excluded from verified/complete.
This is what stops "本地 5,043 ÷ 上游 24 = 210x -> ≥95%" style false positives.

Every normal record carries a `missing` block: count (+ per-kind for wp-api) and a
bounded sample; small diffs keep the full URL list (`missing.urls`), large ones only
keep count + sample so a 260k-post site does not bloat the report.

  python3 collector/verify_coverage.py [--json] [--out PATH] [--site DOMAIN ...]
"""
import argparse, json, os, re, sys
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}

KINDS = ("posts", "pages", "media")
# expected <= local / ANOMALY_RATIO => 上游总量不是同一范围（本地远大于上游）
ANOMALY_RATIO = 1.5
# sitemap 过大（如 lnc 41.6 万条）时不再全量拉取/解析：截断并标"不可验证（sitemap 过大）"，
# 避免一次 verify 卡十几分钟；其余站点不受影响。
SITEMAP_MAX_LOCS = int(os.environ.get("COVERAGE_MAX_LOCS", "60000"))
MISSING_SAMPLE = 20
MISSING_FULL_CAP = 50
ARTICLE_RE = re.compile(r"/20\d\d/|/20\d\d-|/news/|/article|/story|/post|/blog|/press")

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                   "fbclid", "gclid", "ref", "referrer"}


def canonical_key(url):
    """Stable identity for a URL, mirroring crawler.request.canonical_key.

    Kept local on purpose: coverage must not import the queue module just to compare
    strings, and the rule is tiny/stable (lowercase host, no fragment, no trackers,
    no trailing slash).
    """
    try:
        p = urlsplit(url.strip())
    except Exception:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(sorted(query)), ""))


def rows(domain, kind="posts"):
    p = os.path.join(DATA, domain, "urls.txt" if kind == "posts" else f"{kind}.urls.txt")
    try:
        with open(p, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def local_keys(domain, kind="posts"):
    """Canonical keys of the URLs already on disk — the same identity store.dedupe uses."""
    p = os.path.join(DATA, domain, "urls.txt" if kind == "posts" else f"{kind}.urls.txt")
    keys = set()
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                url = line.strip()
                if url:
                    keys.add(canonical_key(url))
    except OSError:
        return keys
    return keys


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
    for kind in KINDS:
        path = f"/?rest_route=/wp/v2/{kind}&per_page=1" if site.get("api") == "rest_route" else f"/wp-json/wp/v2/{kind}?per_page=1"
        try:
            for attempt in (imp, "chrome"):
                r = http(site["base"].rstrip("/") + path, impersonate=attempt)
                if r.status_code == 200:
                    header = r.headers.get("X-WP-Total")
                    out[kind] = int(header) if header not in (None, "") else None
                    break
        except Exception:
            out[kind] = None
    return out


def wp_page_url(site, kind, page, per_page=100):
    """One page of {id, link} pairs, newest first — bounded probe for missing-URL samples."""
    base = site["base"].rstrip("/")
    tail = f"&per_page={per_page}&page={page}&_fields=id,link&orderby=date&order=desc"
    if site.get("api") == "rest_route":
        return f"{base}/?rest_route=/wp/v2/{kind}{tail}"
    return f"{base}/wp-json/wp/v2/{kind}?{tail.lstrip('&')}"


def wp_missing_sample(site, kinds, pages=2, per_page=100):
    """Best-effort missing-URL sample: newest pages*per_page links per kind minus on-disk URLs.

    Bounded on purpose: a 260k-post site would be too expensive to diff fully, and the
    per-kind missing count already comes from X-WP-Total. Returns
    {"sample": [...], "sample_count": n, "scanned": n, "note": str}.
    """
    imp = "chrome" if site.get("via") in ("impersonate", "browser", "cloud") else None
    found, scanned = [], 0
    for kind in kinds:
        known = local_keys(site["domain"], kind)
        for page in range(1, pages + 1):
            try:
                r = None
                for attempt in (imp, "chrome"):
                    r = http(wp_page_url(site, kind, page, per_page), impersonate=attempt)
                    if r.status_code == 200:
                        break
                if r is None or r.status_code != 200:
                    break
                items = r.json()
            except Exception:
                break
            if not isinstance(items, list) or not items:
                break
            scanned += len(items)
            for item in items:
                link = (item or {}).get("link") if isinstance(item, dict) else None
                if link and canonical_key(link) not in known:
                    found.append(link)
    sample = sorted(set(found))[:MISSING_SAMPLE]
    return {"sample": sample, "sample_count": len(sample), "scanned": scanned,
            "note": f"按最新 {scanned} 条抽样（非全量缺失清单）" if scanned else "缺失样本探测失败"}


def sitemap_urls(site, limit_children=25, state=None):
    """Return the list of URLs advertised by the site sitemap.

    state: 可选 dict；sitemap 超过 SITEMAP_MAX_LOCS 时置 state["truncated"]=True。
    """
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
            if len(urls) > SITEMAP_MAX_LOCS:
                urls = urls[:SITEMAP_MAX_LOCS]
                if state is not None:
                    state["truncated"] = True
                break
    filt = site.get("url_filter")
    pat = site.get("link_pattern")
    if filt:
        urls = [u for u in urls if re.search(filt, u)]
    if pat:
        urls = [u for u in urls if re.search(pat, u)]
    if not filt and not pat:                      # only article-shaped URLs count for coverage
        urls = [u for u in urls if ARTICLE_RE.search(u)]
    return list(dict.fromkeys(urls))


# ---------------------------------------------------------------- guard / diff (pure)
def guard(got, expected):
    """(coverage, verdict) for one same-scope comparison; coverage None when expected is unusable.

    verdict: "已核对" (>=95%), "有缺口" (<95%), "口径异常" (expected 缺失/<=0/明显小于本地).
    """
    if isinstance(expected, bool) or not isinstance(expected, (int, float)) or expected <= 0:
        return None, "口径异常"
    if expected <= got / ANOMALY_RATIO:          # 本地明显大于上游 => 上游不是同一范围
        return None, "口径异常"
    cov = got / expected
    return round(cov, 3), ("已核对" if cov >= 0.95 else "有缺口")


def _anomaly_detail(got, expected):
    if not isinstance(expected, (int, float)) or isinstance(expected, bool) or expected <= 0:
        return f"expected 缺失或为 0（本地 {got}），无法按同一范围核对"
    return (f"expected={expected} 明显小于本地={got}（本地/expected={got / expected:.2f}x ≥ "
            f"{ANOMALY_RATIO}x），上游总量与本地不是同一范围")


def missing_urls(expected_urls, known_keys):
    """Upstream URLs whose canonical key is not on disk (order preserved)."""
    out, seen = [], set()
    for url in expected_urls:
        key = canonical_key(url)
        if key in known_keys or key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def missing_payload(items):
    """Count + bounded sample; small diffs keep the full list, large ones do not."""
    n = len(items)
    return {"count": n, "sample": items[:MISSING_SAMPLE],
            "urls": items if n <= MISSING_FULL_CAP else None,
            "truncated": n > MISSING_FULL_CAP}


def _apply_guard(rec, got, expected):
    cov, verdict = guard(got, expected)
    rec["coverage"] = cov
    rec["verdict"] = verdict
    if verdict == "口径异常":
        rec["verdict_detail"] = _anomaly_detail(got, expected)
    return verdict != "口径异常"


def _verify_set(rec, domain, kind, expected_items):
    """sitemap / probed-sitemap: len(loc set) as expected + exact missing-URL diff."""
    rec["expected"] = len(expected_items)
    if not _apply_guard(rec, rec["got"].get(kind, 0), rec["expected"]):
        return
    rec["missing"] = missing_payload(missing_urls(expected_items, local_keys(domain, kind)))


def _verify_wp(site, rec, got):
    totals = wp_totals(site)
    rec["expected"] = totals
    rec["source"] = "X-WP-Total"
    comparable = {k: totals[k] for k in KINDS
                  if isinstance(totals.get(k), int) and not isinstance(totals.get(k), bool)
                  and totals[k] > 0}
    if not comparable:
        rec["verdict"] = "口径异常"
        rec["verdict_detail"] = "X-WP-Total 缺失或为 0（没有同口径上游总量）"
        return
    per_kind, anomalous = {}, {}
    for kind, expected in comparable.items():
        local = got.get(kind, 0)
        if expected <= local / ANOMALY_RATIO:
            anomalous[kind] = {"expected": expected, "got": local, "ratio": round(local / expected, 3)}
        else:
            per_kind[kind] = round(local / expected, 3)
    rec["per_kind"] = per_kind
    if anomalous:                                # 任一 kind 的量级不对，整站不认真核对过
        rec["verdict"] = "口径异常"
        rec["verdict_detail"] = "上游 X-WP-Total 明显小于本地: " + "; ".join(
            f"{k} expected={v['expected']} got={v['got']}（{v['ratio']}x）" for k, v in anomalous.items())
        return
    cov = min(per_kind.values())
    rec["coverage"] = round(cov, 3)
    rec["verdict"] = "已核对" if cov >= 0.95 else "有缺口"
    missing = {k: e - got.get(k, 0) for k, e in comparable.items() if e > got.get(k, 0)}
    payload = {"count": sum(missing.values()), "per_kind": missing,
               "sample": [], "urls": None, "truncated": True}
    if missing:                                  # 只有确有缺口才多打几次接口取样本
        try:
            payload.update(wp_missing_sample(site, list(missing)))
        except Exception as e:
            payload["note"] = f"缺失样本探测失败: {str(e)[:60]}"
    rec["missing"] = payload


def verify_site(s, got):
    """Build one coverage record. Network helpers are module-level so tests can stub them."""
    rec = {"domain": s["domain"], "channel": s["channel"], "got": got,
           "expected": None, "source": None, "coverage": None, "verdict": None}
    try:
        channel = s["channel"]
        if channel == "wp-api":
            _verify_wp(s, rec, got)
        elif channel == "sitemap":
            state = {}
            urls = sitemap_urls(s, state=state)
            rec["source"] = "sitemap <loc>"
            if state.get("truncated"):
                rec["verdict"] = "不可验证"
                rec["verdict_detail"] = (f"sitemap 超过 {SITEMAP_MAX_LOCS} 条已截断"
                                         f"（本地 {got['posts']} 条），需单独抽样核对")
            elif urls:
                _verify_set(rec, s["domain"], "posts", urls)
            else:                                # 该通道本应以 sitemap 为上游，取不到 loc => expected 缺失
                rec["source"] = "sitemap <loc>（未返回 loc）"
                rec["verdict"] = "口径异常"
                rec["verdict_detail"] = "上游 sitemap 未返回任何 <loc>（expected 缺失/为 0），无法按同一范围核对"
        elif channel == "json-api":
            r = http(s["base"].rstrip("/") + s["api"] + "?limit=1&meta=total_count")
            rec["source"] = "Directus meta"
            total = (r.json().get("meta") or {}).get("total_count")
            rec["expected"] = total
            if _apply_guard(rec, got["posts"], total):
                rec["missing"] = {"count": max(int(total) - got["posts"], 0), "sample": [],
                                  "urls": None, "truncated": True,
                                  "note": "上游只给总量，无 URL 列表"}
        else:                                    # rss / html / json-list: probe a sitemap
            state = {}
            urls = [u for u in sitemap_urls(s, state=state) if ARTICLE_RE.search(u)]
            if state.get("truncated"):
                rec["source"] = "sitemap <loc> (truncated)"
                rec["verdict"] = "不可验证"
                rec["verdict_detail"] = f"sitemap 超过 {SITEMAP_MAX_LOCS} 条已截断，需单独抽样核对"
            elif urls:
                rec["source"] = "sitemap <loc> (probed)"
                _verify_set(rec, s["domain"], "posts", urls)
            else:
                rec["source"] = "unverifiable"
                rec["verdict"] = "不可验证"
    except Exception as e:
        rec["coverage"] = None
        rec["verdict"] = "核对失败"
        rec["source"] = f"error: {str(e)[:60]}"
    return rec


def summarize(report, disabled=()):
    by = {}
    for rec in report:
        by[rec.get("verdict")] = by.get(rec.get("verdict"), 0) + 1
    return {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "report": report,
            # verified 只数"有可信上游总量"的站；口径异常 coverage=None，天然不计入
            "verified": sum(1 for r in report if r.get("coverage") is not None),
            "complete": by.get("已核对", 0),
            "partial": by.get("有缺口", 0),
            "anomalous": by.get("口径异常", 0),
            "unverifiable": by.get("不可验证", 0),
            "failed": by.get("核对失败", 0),
            "disabled": len(list(disabled))}


def _expected_str(expected):
    if isinstance(expected, dict):
        return str(sum(v for v in expected.values() if isinstance(v, int)))
    return str(expected) if expected else "-"


def print_table(payload):
    print(f"{'domain':<34}{'channel':<10}{'got':>8}{'expected':>10}{'cov':>7}  verdict  detail")
    for r in sorted(payload["report"], key=lambda x: (x["coverage"] is None, x["coverage"] or 0)):
        cov = f"{r['coverage']:.0%}" if r["coverage"] is not None else "-"
        parts = []
        missing = r.get("missing") or {}
        if missing.get("count"):
            parts.append(f"缺 {missing['count']}" + (
                f"；例 {missing['sample'][0]}" if missing.get("sample") else ""))
        if r.get("per_kind"):
            parts.append(" ".join(f"{k}{v:.0%}" for k, v in r["per_kind"].items()))
        if r.get("verdict_detail"):
            parts.append(r["verdict_detail"])
        detail = "；".join(parts) or (r.get("source") or "")
        print(f"{r['domain']:<34}{r['channel']:<10}{sum(r['got'].values()):>8}"
              f"{_expected_str(r.get('expected')):>10}{cov:>7}  {r.get('verdict') or '-':<6}  {detail}")
    print(f"\n已核对(>=95%) {payload['complete']} 站；有缺口 {payload['partial']}；"
          f"口径异常 {payload['anomalous']}；不可验证 {payload['unverifiable']}；"
          f"核对失败 {payload['failed']}；已跳过 disabled {payload['disabled']} 站")
    print(f"有可信上游总量 {payload['verified']} 站，其中已核对(>=95%) {payload['complete']} 站")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help="报告输出路径（默认 data/coverage_report.json）")
    ap.add_argument("--site", action="append", metavar="DOMAIN",
                    help="只核对指定 domain（可重复；小范围只读抽查用）")
    a = ap.parse_args()
    sites, seen = [], set()
    for s in yaml.safe_load(open(os.path.join(ROOT, "sources.yaml"), encoding="utf-8")) or []:
        domain = s.get("domain")
        if domain and domain not in seen:
            seen.add(domain)
            sites.append(s)
    # disabled = 人工确认不可采（无入口/站点下线/反爬）；核对它们只会制造假残差
    if a.site:
        wanted = set(a.site)
        sites = [s for s in sites if s["domain"] in wanted]
        missing = wanted - {s["domain"] for s in sites}
        if missing:
            sys.exit(f"--site 未在 sources.yaml 中找到: {sorted(missing)}")
    disabled = [s for s in sites if s.get("disabled")]
    report = []
    for s in sites:
        if s.get("disabled"):
            continue
        got = {k: rows(s["domain"], k) for k in KINDS}
        if sum(got.values()) == 0:
            continue
        report.append(verify_site(s, got))

    payload = summarize(report, disabled)
    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print_table(payload)
    out = a.out or os.path.join(DATA, "coverage_report.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out)


if __name__ == "__main__":
    main()
