#!/usr/bin/env python3
"""交付质量报告：posts/pages/media 三类分别按"源站去重 URL"口径算覆盖率。

- 覆盖率数据来自 data/coverage-<date>.json（posts/pages 通常用 X-WP-Total，posts≈唯一 URL）；
- media 若 data/source-counts.json 里记录了唯一 source_url 数，则用该分母（去重口径），
  否则退回 X-WP-Total 的 ID 口径并标注；
- 达标 = 所有有分母的 kind 覆盖率 >= 95%。
输出：output/delivery/<name>/quality.csv
"""
import argparse, csv, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import deliver

def load(path):
    try:
        return json.load(open(path))
    except Exception:
        return {}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20261110")
    ap.add_argument("--sites-file", required=True)
    ap.add_argument("--delivery-name", required=True)
    ap.add_argument("--coverage", default=None)
    a = ap.parse_args()
    cov = {r["domain"]: r for r in load(a.coverage or f"data/coverage-{a.date}.json").get("report", [])}
    unique_media = load("data/source-counts.json").get("media_unique", {})
    doms = [l.strip() for l in open(a.sites_file) if l.strip()]
    out = []
    for d in doms:
        counts = deliver.site_counts("data", d)
        rec = cov.get(d) or {}
        pk = rec.get("per_kind") or {}
        exp = rec.get("expected")
        exp_posts = exp.get("posts") if isinstance(exp, dict) else (exp if isinstance(exp, int) else None)
        def ratio(local, expected):
            return (local / expected) if expected else None
        posts_cov = pk.get("posts")
        pages_cov = pk.get("pages")
        media_cov = pk.get("media")
        basis = "X-WP-Total(ID)"
        if d in unique_media:
            media_cov = ratio(counts["media"], unique_media[d])
            basis = "unique(source_url)"
            if media_cov is not None and media_cov > 1.5:   # 本地远多于源站当前清单 -> 分母不可比
                media_cov = None; basis = "anomalous(source list)"
        if posts_cov is None: posts_cov = ratio(counts["posts"], exp_posts)
        vals = [x for x in (posts_cov, pages_cov, media_cov) if isinstance(x, (int, float))]
        if not vals: status = "入口窗口"
        elif min(vals) >= 0.95: status = "达标"
        else: status = "不足"
        out.append({"domain": d, "posts": counts["posts"], "pages": counts["pages"],
                    "media": counts["media"], "posts_cov": posts_cov, "pages_cov": pages_cov,
                    "media_cov": media_cov, "media_basis": basis, "status": status,
                    "verdict": rec.get("verdict") or "",
                    "note": "无全站总量（小站/按入口范围采集，非漏采）" if status == "入口窗口" else ""})
    cols = ["domain","posts","pages","media","posts_cov","pages_cov","media_cov","media_basis","status","verdict","note"]
    p = Path("output/delivery") / a.delivery_name / "quality.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in out: w.writerow({k: (f"{r[k]:.3f}" if isinstance(r[k], float) else r[k]) for k in cols})
    from collections import Counter
    print("quality.csv ->", p)
    print("status:", dict(Counter(r["status"] for r in out)))
    for r in out:
        if r["status"] != "达标": print(f"  {r['domain']:32} posts={r['posts_cov'] or '-'} pages={r['pages_cov'] or '-'} media={r['media_cov'] or '-'} {r['status']}")

if __name__ == "__main__":
    main()
