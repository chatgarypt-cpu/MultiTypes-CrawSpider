#!/usr/bin/env python3
"""站点接口体检：首页 + JS bundle（可选 HAR）→ 候选接口 → 可粘贴的 sources.yaml 片段。

只读、不改 `sources.yaml`、不写 `data/`。三种输入可以任选其一或叠加：

    python collector/mine_site.py https://example.com/                  # 扫首页内联 + 同站脚本
    python collector/mine_site.py https://example.com/ --har cap.har    # 再加上抓包推断（分页/字段）
    python collector/mine_site.py https://example.com/ --capture        # 现场抓一次包（需 playwright）

输出：候选表（Markdown）+ 每个候选的 sources.yaml 片段 + schema 校验结果。
`--json` 给机器读（候选数组 + suggestions），CI / 工作台可直接消费。

设计取舍：这里只做"体检"，不自动改配置——`to_sources_entry()` 的 `api_url` / `api_items`
是推断值，建议在试采中验证。
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from crawler import contract, endpoints, schema  # noqa: E402
from crawler.public_urls import normalize_url  # noqa: E402
from crawler.web import Fetcher  # noqa: E402


def _fetch_home(url, timeout=25):
    """取首页；失败只返回空串（挖接口是加分项，不该让体检本身失败）。"""
    host = urlsplit(url).netloc
    for got_url, status, body in Fetcher({"domain": host, "_public_host": host}).get_many([url], timeout=timeout):
        if status == 200 and isinstance(body, str):
            return got_url, body
        print(f"[warn] 首页取不到：{got_url} status={status}", file=sys.stderr)
    return url, ""


def _scan(url, html, limit=12):
    """首页 HTML → endpoints.scan（带同站脚本 fetcher）。"""
    if not html:
        return {"candidates": [], "scripts": 0, "via": "python"}
    host = urlsplit(url).netloc
    fetcher = Fetcher({"domain": host, "_public_host": host})
    try:
        return endpoints.scan(html, url, fetcher=fetcher, limit=limit)
    except Exception as exc:                      # 拉脚本失败不影响内联脚本的候选
        print(f"[warn] 脚本扫描失败：{exc}", file=sys.stderr)
        return endpoints.scan(html, url, limit=limit)


def _from_har_arg(value):
    path = Path(value)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"HAR 读不了：{path}（{exc}）")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description="站点接口体检：挖接口 + 推断候选契约")
    parser.add_argument("url", help="/ 或任意页面地址")
    parser.add_argument("--har", help="已有 HAR 文件（Playwright/CDP 导出的 log.entries 结构）")
    parser.add_argument("--capture", action="store_true", help="现场抓一次包（需要 Playwright）")
    parser.add_argument("--limit", type=int, default=12, help="最多拉几个外链脚本（默认 12）")
    parser.add_argument("--domain", help="写进 sources.yaml 的 domain（默认取 URL 主机）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    url = normalize_url(args.url)
    home_url, html = _fetch_home(url)
    host = urlsplit(url).netloc
    notes = []

    scanned = _scan(home_url, html, limit=args.limit)
    candidates = list(scanned.get("candidates") or [])
    notes.append(f"扫脚本 {scanned.get('scripts', 0)} 个（{scanned.get('via', 'python')}）")

    har = None
    if args.capture:
        try:
            from crawler import capture
            har = capture.capture(home_url, public_host=host)
            notes.append("抓包来源：浏览器")
        except Exception as exc:
            notes.append(f"抓包跳过：{str(exc)[:80]}")
    if args.har:
        har = _from_har_arg(args.har)
        notes.append("抓包来源：HAR 文件")
    # endpoints 的裸候选先转成契约结构，再和抓包候选合并（两类字段不同，统一到 from_* 的产物）
    ranked = contract.from_endpoints(candidates)
    if har:
        try:
            from_har = contract.from_har(har, hosts=[host], min_bytes=10)
            ranked = ranked + from_har
            notes.append(f"HAR 候选 {len(from_har)} 条")
        except Exception as exc:
            notes.append(f"HAR 解析失败：{str(exc)[:80]}")
    # 同一接口可能既被挖到（无字段）又被抓包命中（有分页/字段）：按 (method, host, path_template)
    # 合并，保留分数高（信息更全）的那条，避免候选表里出现两行同一个接口。
    best = {}
    for candidate in ranked:
        if not isinstance(candidate, dict):
            continue
        key = (candidate.get("method"), candidate.get("host"), candidate.get("path_template"))
        current = best.get(key)
        if current is None or (candidate.get("score") or 0) > (current.get("score") or 0):
            best[key] = candidate
    ranked = sorted(best.values(), key=lambda c: -(c.get("score") or 0))

    suggestions = []
    for candidate in ranked:
        try:
            entry = contract.to_sources_entry(candidate, domain=args.domain or host)
        except ValueError:
            continue
        errors, warnings = schema.validate_site(entry)
        suggestions.append({"entry": entry, "errors": errors, "warnings": warnings})

    if args.json:
        print(json.dumps({"url": url, "notes": notes, "candidates": ranked,
                          "suggestions": suggestions}, ensure_ascii=False, indent=2))
        return 0

    print(f"# {url}\n")
    print("、".join(notes) or "（无）")
    print()
    print(contract.render(ranked))
    for index, item in enumerate(suggestions[:3], 1):
        print(f"\n## 候选 {index}：sources.yaml\n")
        print(yaml.safe_dump(item["entry"], allow_unicode=True, sort_keys=False).rstrip())
        if item["errors"]:
            print("\n! schema errors: " + "; ".join(item["errors"]))
        if item["warnings"]:
            print("  warnings: " + "; ".join(item["warnings"]))
    if not suggestions:
        print("\n（没挖到可用接口：这批站可能本来就没有接口，考虑 wp-api / sitemap / rss / html 通道）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
