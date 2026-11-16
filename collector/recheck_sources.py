#!/usr/bin/env python3
"""重新调研"死站/未配置/WAF"信源：直连 + Camoufox headless，输出 CSV。"""
import argparse, csv, re, sys, time
from datetime import datetime
from pathlib import Path
import yaml
import requests
from urllib.parse import urlsplit
sys.path.insert(0, 'collector')
from crawler.browser_camoufox import fetch_many
from crawler.htmlpage import page_error

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
DEAD_MARKERS = ("account suspended", "domain for sale", "buy this domain",
                "this domain is for sale", "domain parked", "parked domain")
CHALLENGE = ("robot challenge", "sgcaptcha", "one moment", "just a moment", "请稍候", "cf-challenge")

def title_of(html):
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:90] if m else ""

def verdict(status, html, final_url, domain):
    low = (html or "").lower()
    if status == 0:
        return "unreachable", "当前出口连接失败，未确认站点下线"
    if page_error(html) == "challenge":
        return "blocked", "挑战页"
    if status in (401, 403, 429):
        return "blocked", f"HTTP {status}，当前出口受限"
    if status >= 400:
        return "http_error", f"HTTP {status}，需核对有效入口"
    title = title_of(html).lower()
    if any(m in title for m in DEAD_MARKERS) or "cgi-sys/suspendedpage" in (final_url or ""):
        return "unavailable", "停放/挂起页面，未确认永久下线"
    if any(m in title for m in ("page not found", "404 not found", "site not found", "coming soon", "under construction")):
        return "needs_review", "入口错误或建设中，需核对有效页面"
    if len(html or "") < 2000:
        return "needs_review", f"空/过短 {len(html or '')}B，需核对跳转与页面身份"
    host = (urlsplit(final_url or "").hostname or "").lower()
    if host and host.removeprefix("www.") != domain.lower().removeprefix("www."):
        return "redirect", f"跳转到 {host}"
    return "alive", "有内容"

def direct(domain):
    for scheme in ("https://", "http://"):
        try:
            r = requests.get(scheme + domain, timeout=12, headers={"User-Agent": UA}, allow_redirects=True)
            return r.status_code, r.text or "", r.url
        except Exception as e:
            last = (0, "", str(e)[:80])
    return last

def main():
    root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=root / "output/reports/original-109-status-20261113.csv")
    ap.add_argument("--out", type=Path, default=root / f"output/reports/recheck-{datetime.now():%Y%m%d-%H%M%S}.csv")
    args = ap.parse_args()
    rows = list(csv.DictReader(args.input.open(encoding="utf-8-sig")))
    sites = yaml.safe_load((root / "collector/sources.yaml").read_text())
    aliases = {alias: s["domain"] for s in sites for alias in s.get("aliases", [])}
    excluded = {s["domain"] for s in sites if s.get("disabled") and "非本地主体" in s.get("disabled_reason", "")}
    seen, doms = set(), []
    for r in rows:
        d = r.get("当前域名") or r["原域名"]
        d = aliases.get(d, aliases.get(r["原域名"], d))
        if d in excluded:
            continue
        if r["状态"] in ("不采", "未采") and d not in seen:
            seen.add(d); doms.append((d, r["中文名"]))
    out, need = [], []
    for d, name in doms:
        st, html, url = direct(d)
        v, note = verdict(st, html, url, d)
        rec = {"domain": d, "name": name, "direct_status": st, "direct_title": title_of(html),
               "direct_len": len(html or ""), "final_url": url, "cf_status": "", "cf_title": "",
               "cf_len": "", "verdict": v, "note": note}
        out.append(rec)
        if v != "alive":
            need.append((d, rec))
        print(d, st, v, rec["direct_title"][:50], flush=True)
    print(f"--- camoufox for {len(need)} ---", flush=True)
    if need:
        urls = ["https://" + d for d, _ in need]
        res = {u: (st, h) for u, st, h in fetch_many(urls, wait_ms=6000)}
        for d, rec in need:
            st, h = res.get("https://" + d, (0, ""))
            rec["cf_status"] = st; rec["cf_title"] = title_of(h); rec["cf_len"] = len(h or "")
            v, note = verdict(st, h, "https://" + d, d)
            if v == "alive":
                rec["verdict"], rec["note"] = "alive", "Camoufox 拿到内容"
            elif v == "blocked" and rec["verdict"] != "blocked":
                rec["verdict"], rec["note"] = "blocked", "Camoufox 仍挑战页"
    if not out:
        print("No eligible sources to recheck")
        return
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0])); w.writeheader(); w.writerows(out)
    print("written", len(out))

if __name__ == "__main__":
    main()
