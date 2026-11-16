#!/usr/bin/env python3
"""站点普查：首页菜单 → 栏目 → 分页 → 详情页计数，产出候选采集配置。

用途：2026-11-12 起"全站详情页"规则落地前，先看清每个站的结构，
不直接改 sources.yaml。

用法：
  uv run python collector/survey_site.py --site www.gov.vu
  uv run python collector/survey_site.py --base https://www.fwrm.org.fj --out md
  uv run python collector/survey_site.py --site www.pngnri.org --probe 2 --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import yaml
from lxml import html as lxml_html

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from crawler.web import _get, headers_for  # noqa: E402

ASSET = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|json|xml|pdf|zip|mp4|mp3|woff2?|ttf|eot)(?:$|\?)", re.I)
SKIP = re.compile(r"(?:/administrator/|/wp-admin/|/login|/search|/tags?/|/author/|/feed\b|/rss\b|mailto:|javascript:|^#)", re.I)
PAG = [
    (re.compile(r"[?&]start=(\d+)"), "start"),
    (re.compile(r"[?&]page=(\d+)"), "page"),
    (re.compile(r"[?&]p=(\d+)"), "p"),
    (re.compile(r"[?&]offset=(\d+)"), "offset"),
    (re.compile(r"/page/(\d+)"), "path_page"),
    (re.compile(r"\(offset\)/(\d+)"), "offset_path"),
]
DETAIL_HINT = re.compile(r"(?:/news|/article|/story|/press|/report|/publication|/actualite|/communique|/blog|/updates)", re.I)
BOILER = re.compile(r"(?:contact|mentions-legales|protection-des-donnees|cookies?|plan-du-site|accessibilite|login|connexion|accueil|^/fr/?$)", re.I)


def load_site(domain: str, base: str | None) -> dict:
    if base:
        return {"domain": urlsplit(base).netloc, "base": base.rstrip("/"), "via": "direct"}
    sites = yaml.safe_load(open(ROOT / "sources.yaml"))
    for s in sites:
        if s["domain"] == domain:
            return s
    raise SystemExit(f"{domain} not in sources.yaml; use --base")


def fetch(site: dict, url: str) -> tuple[int, str]:
    st, body, _ = _get(None, url, timeout=25, attempts=2,
                       impersonate=site.get("via") == "impersonate",
                       headers=headers_for(site))
    return st, body or ""


def fetch_many(site: dict, urls: list[str], workers: int = 8) -> list[tuple[int, str]]:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(lambda u: fetch(site, u), urls))


def same_host(url: str, host: str) -> bool:
    return urlsplit(url).netloc == host


def clean(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme or "https", p.netloc, p.path, p.query, ""))


def menu_links(doc, base: str) -> list[tuple[str, str]]:
    host = urlsplit(base).netloc
    nodes = doc.xpath("//nav//a[@href] | //*[contains(@class,'menu')]//a[@href] | "
                      "//*[contains(@id,'menu')]//a[@href] | //header//a[@href]")
    if not nodes:
        nodes = doc.xpath("//a[@href]")
    out, seen = [], set()
    for a in nodes:
        href = (a.get("href") or "").strip()
        if not href or SKIP.search(href) or ASSET.search(href):
            continue
        full = clean(urljoin(base, href))
        if not full.startswith("http") or not same_host(full, host) or full == base:
            continue
        if full in seen:
            continue
        seen.add(full)
        text = re.sub(r"\s+", " ", a.text_content()).strip()
        out.append((text, full))
    return out


def detail_links(doc, page_url: str, section: str, menu: set[str]) -> list[str]:
    host = urlsplit(section).netloc
    sec_path = urlsplit(section).path.rstrip("/")
    parent = sec_path.rsplit("/", 1)[0] if "/" in sec_path.strip("/") else ""
    out, seen = [], set()
    for a in doc.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        if not href or SKIP.search(href) or ASSET.search(href):
            continue
        full = clean(urljoin(page_url, href))
        if not full.startswith("http") or not same_host(full, host):
            continue
        if full in menu or full == clean(section) or full in seen:
            continue
        if BOILER.search(full):
            continue
        if any(r.search(full) for r, _ in PAG):        # 分页链接
            continue
        path = urlsplit(full).path
        last = path.rstrip("/").rsplit("/", 1)[-1]
        if not (re.search(r"\d{2,}", last) or len(re.findall(r"[-_]", last)) >= 2 or DETAIL_HINT.search(path)):
            continue
        seen.add(full)
        out.append(full)
    return out


def pagination(doc, page_url: str) -> dict:
    """从栏目页提取分页模板、步长和 'Page X of Y'。"""
    text = re.sub(r"\s+", " ", doc.text_content())
    total = None
    m = re.search(r"page\s+(\d+)\s+of\s+(\d+)", text, re.I)
    if m:
        total = int(m.group(2))
    found = []
    for a in doc.xpath("//a[@href]"):
        href = a.get("href") or ""
        if href.startswith(("http", "//")) and not same_host(href, urlsplit(page_url).netloc):
            continue
        for rx, kind in PAG:
            mm = rx.search(href)
            if mm:
                val = int(mm.group(1))
                tmpl = clean(urljoin(page_url, href[:mm.start(1)] + "{page}" + href[mm.end(1):]))
                found.append((kind, val, tmpl))
    if not found:
        return {"kind": None, "step": None, "template": None, "total_pages": total}
    # 取出现次数最多的 kind；步长取最小的正数
    kinds = sorted({f[0] for f in found})
    kind = kinds[0]
    vals = sorted({v for k, v, _ in found if k == kind and v > 0})
    step = vals[0] if vals else 1
    tmpl = next(t for k, v, t in found if k == kind and v == step)
    return {"kind": kind, "step": step, "template": tmpl, "total_pages": total, "all_kinds": kinds}


def survey(site: dict, max_sections: int, probe: int) -> dict:
    base = site["base"].rstrip("/")
    st, body = fetch(site, base)
    if st != 200 or not body:
        return {"domain": site["domain"], "base": base, "error": f"home {st}"}
    doc = lxml_html.fromstring(body)
    menu = menu_links(doc, base)
    menu_set = {u for _, u in menu}
    seen = set()
    sections = []
    for t, u in menu:
        if t and u not in seen:
            seen.add(u)
            sections.append((t, u))
    sections = sections[:max_sections]
    results = fetch_many(site, [u for _, u in sections])
    rows = []
    for (title, url), (st, body) in zip(sections, results):
        row = {"title": title, "url": url, "status": st}
        if st == 200 and body:
            d = lxml_html.fromstring(body)
            details = detail_links(d, url, url, menu_set)
            pg = pagination(d, url)
            row.update({"details_p1": len(details), "samples": details[:5], **pg})
            row["_details"] = details
        rows.append(row)
    if probe > 0:
        probes = [(i, r) for i, r in enumerate(rows) if r.get("template")]
        fetched = fetch_many(site, [r["template"].replace("{page}", str(r["step"])) for _, r in probes])
        for (i, r), (st2, body2) in zip(probes, fetched):
            next_url = r["template"].replace("{page}", str(r["step"]))
            r["probe_url"] = next_url
            if st2 == 200 and body2:
                d2 = lxml_html.fromstring(body2)
                more = detail_links(d2, next_url, r["url"], menu_set)
                r["details_p2"] = len(more)
                r["probe_new"] = len([u for u in more if u not in set(r.get("_details") or [])])
                r["samples_p2"] = more[:3]
            else:
                r["details_p2"] = None
                r["probe_error"] = st2
    for r in rows:
        r.pop("_details", None)
    return {"domain": site["domain"], "base": base, "home_menu": len(menu), "sections": rows}


def render_md(r: dict) -> str:
    if r.get("error"):
        return f"# 站点普查 · {r['domain']}\n\n- base: {r['base']}\n- 失败: {r['error']}\n"
    lines = [f"# 站点普查 · {r['domain']}", "", f"- base: {r['base']}", f"- 首页菜单链接: {r['home_menu']}", ""]
    for s in r["sections"]:
        lines.append(f"## {s['title']} — `{s['url']}`")
        lines.append(f"- 状态: {s['status']}")
        if s.get("details_p1") is not None:
            lines.append(f"- 第 1 页详情链接: **{s['details_p1']}**")
        if s.get("kind"):
            lines.append(f"- 分页: `{s['kind']}` 步长 {s['step']}，总页数: {s.get('total_pages') or '未知'}")
            lines.append(f"- 模板: `{s['template']}`")
        else:
            lines.append("- 分页: 未发现")
        if s.get("details_p2") is not None:
            lines.append(f"- 第 2 页: {s['details_p2']} 条（新增 {s.get('probe_new')}）")
        if s.get("probe_error"):
            lines.append(f"- 第 2 页失败: {s['probe_error']}")
        for u in s.get("samples", [])[:3]:
            lines.append(f"  - {u}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site")
    ap.add_argument("--base")
    ap.add_argument("--max-sections", type=int, default=30)
    ap.add_argument("--probe", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    site = load_site(args.site or "", args.base)
    r = survey(site, args.max_sections, args.probe)
    out = json.dumps(r, ensure_ascii=False, indent=2)
    p = ROOT.parent / "output" / "reports" / f"survey-{site['domain'].replace(':','_')}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(out, encoding="utf-8")
    print(out if args.json else render_md(r))
    print(f"\n(json -> {p})" if not args.json else f"json -> {p}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
