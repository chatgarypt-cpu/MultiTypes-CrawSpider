#!/usr/bin/env python3
"""A/B 基准：把"浏览器/降噪库能带来多少收益"变成可复现的数字（只读，不写 data/）。

两种模式：
  transport  同一批 URL，比较 direct / impersonate / ego 三种出口的 成功率、耗时、可用正文
  extract    同一份 HTML，比较 trafilatura(markdown) / readability(+markdownify) / 整页粗抽
             的 正文长度、链接密度、压缩率、标题/日期完整率

示例：
  python collector/bench_ab.py transport --site example.org --n 8 --methods direct,impersonate,ego
  python collector/bench_ab.py extract --site example.org --n 8 --fetch-via direct
  # readability 需要临时依赖：uv run --with readability-lxml --with markdownify python collector/bench_ab.py extract ...
"""
import argparse
import json
import os
import sys
import time

from crawler.config import sources_path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import yaml  # noqa: E402

import trafilatura  # noqa: E402

from crawler import htmlpage  # noqa: E402
from crawler.extract import extract_best  # noqa: E402
from crawler.text import clean, meta  # noqa: E402
from crawler.web import Fetcher, get_text, headers_for  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}


def load_site(domain):
    sites = yaml.safe_load(open(sources_path(HERE), encoding="utf-8")) or []
    sit = next((s for s in sites if s.get("domain") == domain), None)
    if sit is None:
        sys.exit(f"site {domain!r} not in sources.yaml")
    return sit


def sample_urls(domain, n, kind="posts"):
    p = os.path.join(HERE, "data", domain, "urls.txt" if kind == "posts" else f"{kind}.urls.txt")
    urls = []
    if os.path.exists(p):
        urls = [l.strip() for l in open(p, encoding="utf-8") if l.strip()]
    urls = [u for u in urls if "page:" not in u and "?start=" not in u]
    return urls[-n:] if urls else []


# ---------------- transport A/B ----------------
def fetch_direct(url, site):
    t = time.time()
    st, body = get_text(url, headers_for(site), timeout=(10, 25))
    return st, body, time.time() - t


def fetch_impersonate(url, site):
    from curl_cffi import requests as cffi
    t = time.time()
    try:
        r = cffi.get(url, impersonate="chrome", timeout=45, headers={"User-Agent": UA["User-Agent"]})
        return r.status_code, r.text, time.time() - t
    except Exception as e:
        return 0, str(e)[:120], time.time() - t


def fetch_playwright(urls, site, block=False):
    import crawler.browser_playwright as pw
    t = time.time()
    out = {}
    for u, st, body in pw.fetch_many(urls, wait_ms=site.get("playwright_wait_ms") or 3000,
                                     block_resources=block, user_agent=UA["User-Agent"]):
        out[u] = (st, body, time.time() - t)
    return out


def fetch_ego(urls, site):
    import crawler.browser_ego as ego
    t = time.time()
    out = {}
    for u, st, body in ego.fetch_many(urls, wait_ms=site.get("ego_wait_ms") or 3000, timeout=300):
        out[u] = (st, body, time.time() - t)
    return out


def _usable(body, site):
    """返回 (可用正文长度, 标题, 日期)：用项目当前抽取链路判断，和线上口径一致。"""
    if not body:
        return 0, "", ""
    row = extract_best("https://bench.test/x", body, site, min_text=0)
    if not row:
        return 0, "", ""
    return len(row.get("text") or ""), (row.get("title") or ""), (row.get("created_at") or "")


def cmd_transport(args):
    site = load_site(args.site)
    urls = sample_urls(args.site, args.n)
    if not urls:
        sys.exit("样本不足：data/<domain>/urls.txt 里没有 URL")
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    rows = []
    ego_all = fetch_ego(urls, site) if "ego" in methods else {}
    pw_all = fetch_playwright(urls, site) if "playwright" in methods else {}
    pwb_all = fetch_playwright(urls, site, block=True) if "playwright-block" in methods else {}
    for u in urls:
        rec = {"url": u}
        for m in methods:
            try:
                if m == "direct":
                    st, body, secs = fetch_direct(u, site)
                elif m == "impersonate":
                    st, body, secs = fetch_impersonate(u, site)
                elif m == "ego":
                    st, body, secs = ego_all.get(u, (0, "", 0))
                elif m == "playwright":
                    st, body, secs = pw_all.get(u, (0, "", 0))
                elif m == "playwright-block":
                    st, body, secs = pwb_all.get(u, (0, "", 0))
                else:
                    raise ValueError(m)
            except Exception as e:
                st, body, secs = 0, str(e)[:120], 0
            text_len, title, date = _usable(body, site)
            per_page = round(secs / max(1, len(urls)), 2) if m in ("ego", "playwright", "playwright-block") else round(secs, 2)
            rec[m] = {"http": st, "bytes": len(body or ""), "secs": per_page,
                      "text_len": text_len, "title": bool(title), "date": bool(date),
                      "ok": st == 200 and text_len >= 120}
        rows.append(rec)
    print(json.dumps({"site": args.site, "n": len(rows), "methods": methods, "rows": rows},
                     ensure_ascii=False))
    # markdown 汇总
    print("\n| method | 平均耗时 s | 成功率(202+正文≥120) | 平均正文长度 | 标题完整 | 日期完整 | 平均字节 |")
    print("|---|---|---|---|---|---|---|")
    for m in methods:
        vals = [r[m] for r in rows]
        n_ok = sum(1 for v in vals if v["ok"])
        avg_secs = round(sum(v["secs"] for v in vals) / max(1, len(vals)), 2)
        avg_text = int(sum(v["text_len"] for v in vals) / max(1, len(vals)))
        avg_bytes = int(sum(v["bytes"] for v in vals) / max(1, len(vals)))
        print(f"| {m} | {avg_secs} | {n_ok}/{len(vals)} | {avg_text} | "
              f"{sum(1 for v in vals if v['title'])}/{len(vals)} | "
              f"{sum(1 for v in vals if v['date'])}/{len(vals)} | {avg_bytes} |")


# ---------------- extract A/B ----------------
def readability_text(body):
    try:
        from readability import Document
        from markdownify import markdownify as md
    except Exception:
        return None
    try:
        summary = Document(body).summary(html_partial=True)
        return md(summary)
    except Exception:
        return ""


def cmd_extract(args):
    site = load_site(args.site)
    urls = sample_urls(args.site, args.n)
    if not urls:
        sys.exit("样本不足：data/<domain>/urls.txt 里没有 URL")
    fetcher = Fetcher(dict(site, via=args.fetch_via)) if args.fetch_via else Fetcher(site)
    rows = []
    for u, st, body in fetcher.stream_many(urls, workers=1):
        if st != 200 or not body:
            continue
        raw_len = len(body)
        # 1) 整页粗抽（基线）
        crude = clean(body)
        # 2) trafilatura 纯文本 / markdown
        plain = trafilatura.extract(body) or ""
        md_t = htmlpage.markdown(body, u)
        # 3) readability + markdownify
        rd = readability_text(body)
        def link_ratio_of(text_or_md):
            # 粗略：markdown 里 [text](url) 的 text 占比
            import re as _re
            links = _re.findall(r"\[([^\]]+)\]\([^)]+\)", text_or_md or "")
            chars = len(text_or_md or "")
            return round(sum(len(x) for x in links) / chars, 3) if chars else 0.0
        rows.append({
            "url": u, "raw_html": raw_len,
            "trafilatura_plain": {"chars": len(plain), "link_ratio": link_ratio_of(plain)},
            "trafilatura_md": {"chars": len(md_t), "link_ratio": link_ratio_of(md_t)},
            "readability_md": ({"chars": len(rd), "link_ratio": link_ratio_of(rd)}
                               if rd is not None else None),
            "crude_clean": {"chars": len(crude), "link_ratio": 0},
        })
    print(json.dumps({"site": args.site, "n": len(rows), "rows": rows}, ensure_ascii=False))
    print("\n| extractor | 平均字符 | 相对原始HTML压缩率 | 平均链接密度 | 备注 |")
    print("|---|---|---|---|---|")
    raw = sum(r["raw_html"] for r in rows) / max(1, len(rows))
    for key, note in (("trafilatura_plain", ""), ("trafilatura_md", "含格式标记"),
                      ("readability_md", "需 readability-lxml + markdownify"),
                      ("crude_clean", "整页文字，含导航")):
        vals = [r[key] for r in rows if r.get(key)]
        if not vals:
            print(f"| {key} | - | - | - | 未安装/不可用 |")
            continue
        avg = sum(v["chars"] for v in vals) / len(vals)
        avg_link = sum(v["link_ratio"] for v in vals) / len(vals)
        print(f"| {key} | {int(avg)} | {round(1 - avg / raw, 3):.1%} | {avg_link:.2f} | {note} |")


def main():
    ap = argparse.ArgumentParser(description="crawler A/B benchmark (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("transport")
    p.add_argument("--site", required=True)
    p.add_argument("--n", type=int, default=8)
    p.add_argument("--methods", default="direct,impersonate,ego")
    p.set_defaults(func=cmd_transport)
    p = sub.add_parser("extract")
    p.add_argument("--site", required=True)
    p.add_argument("--n", type=int, default=8)
    p.add_argument("--fetch-via", default="")
    p.set_defaults(func=cmd_extract)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
