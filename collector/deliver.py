#!/usr/bin/env python3
"""交付流水线：一条命令产出 CSV 与交付目录四件套。

  python3 collector/deliver.py                          # 今天，导出 CSV + 交付目录
  python3 collector/deliver.py --date 20261107 --no-export
  python3 collector/deliver.py --dry-run                # 只打印计划，不写任何文件

产出：
  output/csv/<domain>[.pages|.media].csv                 （复用 export_csv.export）
  output/delivery/<yyyymmdd>/site_manifest.csv           站点清单
  output/delivery/<yyyymmdd>/residuals.csv               残差清单
  output/delivery/<yyyymmdd>/交付说明.md                 人读版交付说明
  output/delivery/<yyyymmdd>/coverage.json               覆盖率快照（归档）

残差/覆盖率读 data/coverage-<yyyymmdd>.json（指定日期不存在则回退到最新
coverage-*.json 并提示）；行数取 data/<domain>/*.urls.txt 的当前行数。
没有覆盖率快照时仍可出清单，但会提示先跑 `collectorctl verify`。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # collector/
REPO = ROOT.parent
SOURCES = ROOT / "sources.yaml"
EXCLUDE_DOMAINS = {"blog.python.org"}          # 本地测试站，不进交付清单
DATA = ROOT / "data"                            # collector/data -> ../data
CSV_DIR = REPO / "output" / "csv"
DELIVERY_ROOT = REPO / "output" / "delivery"

KINDS = ("posts", "pages", "media")
MANIFEST_COLS = ["domain", "name", "country", "type", "channel", "posts", "pages",
                 "media", "total", "coverage", "missing", "status", "note", "files"]
RESIDUAL_COLS = ["domain", "kind", "got", "expected", "missing", "ratio",
                 "channel", "verdict", "sampled"]
SITE_MARKERS = ("urls.txt", "articles.jsonl", "pages.jsonl", "media.jsonl",
                "cursor.json", "pages.cursor.json", "media.cursor.json")


# ---------------------------------------------------------------- helpers
def _default_data_dir() -> Path:
    """默认数据目录：跟随 crawler.config.DATA（测试里会被 conftest 指到 tmp）。"""
    try:
        from crawler import config
        return Path(config.DATA)
    except Exception:
        return DATA


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _write_csv(path: Path, cols, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    os.replace(tmp, path)


def count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _lines(data_dir: Path, domain: str, kind: str) -> int:
    d = Path(data_dir) / domain
    if kind == "posts":
        primary, fallback = d / "urls.txt", d / "articles.jsonl"
    else:
        primary, fallback = d / f"{kind}.urls.txt", d / f"{kind}.jsonl"
    if primary.exists():
        return count_lines(primary)
    return count_lines(fallback)


def site_counts(data_dir, domain: str) -> dict:
    return {kind: _lines(data_dir, domain, kind) for kind in KINDS}


def site_dirs(data_dir) -> list[str]:
    """data/ 下看起来像站点目录的子目录（有 urls/jsonl/cursor 任一标记）。"""
    data = Path(data_dir)
    if not data.is_dir():
        return []
    out = []
    for p in sorted(data.iterdir()):
        if not p.is_dir() or p.is_symlink():
            continue
        if any((p / m).exists() for m in SITE_MARKERS):
            out.append(p.name)
    return out


def load_sources(path=None) -> list[dict]:
    """读 sources.yaml，按 domain 去重（重复 key 由 T3 的 schema 校验负责报错）。"""
    src = Path(path) if path else SOURCES
    try:
        import yaml
        payload = yaml.safe_load(src.read_text(encoding="utf-8")) or []
    except FileNotFoundError:
        return []
    except Exception as exc:                    # yaml 缺失 / 文件损坏 → 只警告，清单仍可出
        print(f"⚠ 读取站点配置失败（{src}）：{exc}", file=sys.stderr)
        return []
    out, seen = [], set()
    for site in payload if isinstance(payload, list) else []:
        dom = (site or {}).get("domain")
        if dom and dom not in seen:
            seen.add(dom)
            out.append(site)
    return out


def read_skip_keys(data_dir) -> set[str]:
    """data/CRAWL_SKIP 里 `domain:kind` 形式的用户暂停任务。"""
    p = Path(data_dir) / "CRAWL_SKIP"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}


def find_coverage(data_dir, date: str | None = None):
    """返回 (json 路径或 None, 提示语或 None)。指定日期优先，否则用最新快照。"""
    data = Path(data_dir)
    if date:
        exact = data / f"coverage-{date}.json"
        if exact.exists():
            return exact, None
    candidates = sorted(data.glob("coverage-*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if date and candidates:
        return candidates[0], f"未找到 data/coverage-{date}.json，改用最新快照 {candidates[0].name}"
    if candidates:
        return candidates[0], None
    return None, None


def load_coverage(data_dir, date=None):
    """返回 (payload, path, warning)。payload 形如 {"report": [...], "verified": n, ...}。"""
    path, warning = find_coverage(data_dir, date)
    if path is None:
        return None, None, warning
    payload = _read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("report"), list):
        return None, path, f"覆盖率快照格式不可用：{path.name}"
    return payload, path, warning


# ---------------------------------------------------------------- manifest
def _coverage_records(payload) -> dict:
    if not payload:
        return {}
    return {rec.get("domain"): rec for rec in payload.get("report", []) if rec.get("domain")}


def _expected_by_kind(rec) -> dict:
    """wp-api 的 expected 是 {kind: n}；sitemap/json-api 是单值（只算 posts）。"""
    expected = rec.get("expected")
    if isinstance(expected, dict):
        return {k: v for k, v in expected.items() if isinstance(v, int) and v}
    if isinstance(expected, int) and expected:
        return {"posts": expected}
    return {}


def expected_missing(rec, counts: dict) -> int:
    return sum(max(exp - counts.get(kind, 0), 0)
               for kind, exp in _expected_by_kind(rec or {}).items())


def _status_note(domain, counts, total, coverage, missing, skip_keys) -> tuple[str, str]:
    paused = [k for k in KINDS if f"{domain}:{k}" in skip_keys]
    if paused:
        return "暂停", f"用户暂停（data/CRAWL_SKIP）：{','.join(paused)}"
    if total == 0:
        return "无数据", "无产出（死站/无内容/未开始）"
    if missing > 0:
        return "有残差", "与独立总量口径有差额，见 residuals.csv"
    if coverage is None:
        return "无法验证", "无独立总量源"
    return "完成", ""


def _csv_files(csv_dir, domain) -> str:
    d = Path(csv_dir)
    names = [f"{domain}.csv", f"{domain}.pages.csv", f"{domain}.media.csv"]
    return ",".join(n for n in names if (d / n).exists())


def build_manifest(sources, data_dir, coverage_payload, csv_dir, skip_keys) -> list[dict]:
    """一行一个站点：配置站点在前（sources.yaml 顺序），未配置但有数据的目录追加在后。"""
    records = _coverage_records(coverage_payload)
    by_domain = {(s or {}).get("domain"): s for s in sources}
    configured = [s["domain"] for s in sources if (s or {}).get("domain")]
    # 测试/临时站点不进交付清单
    extras = [d for d in site_dirs(data_dir) if d not in by_domain and d not in EXCLUDE_DOMAINS]
    rows = []
    for domain in configured + extras:
        cfg = by_domain.get(domain) or {}
        counts = site_counts(data_dir, domain)
        total = sum(counts.values())
        rec = records.get(domain)
        coverage = rec.get("coverage") if rec else None
        missing = expected_missing(rec, counts)
        status, note = _status_note(domain, counts, total, coverage, missing, skip_keys)
        if domain in extras:
            note = (note + "；" if note else "") + "未在 sources.yaml 配置（adhoc/测试）"
        rows.append({
            "domain": domain,
            "name": cfg.get("name", ""),
            "country": cfg.get("country", ""),
            "type": cfg.get("type", ""),
            "channel": cfg.get("channel", ""),
            "posts": counts["posts"],
            "pages": counts["pages"],
            "media": counts["media"],
            "total": total,
            "coverage": "" if coverage is None else coverage,
            "missing": missing,
            "status": status,
            "note": note,
            "files": _csv_files(csv_dir, domain),
        })
    return rows


# ---------------------------------------------------------------- residuals
def _verdict(channel: str) -> str:
    if channel == "wp-api":
        return "X-WP-Total 口径差额：源站不可读/重复分页/过滤，非未抓取"
    return "非 wp-api 通道；口径含多语/栏目页，非未抓取"


def build_residuals(coverage_payload, data_dir) -> list[dict]:
    """残差 = 覆盖率快照 expected 与当前 urls 行数的差额；无总量源的站点单独标注。

    只输出有缺口的 kind（missing > 0）；快照里无法验证的站点输出一行
    verdict=无法验证（expected/missing/ratio 留空）。排序：缺量大的在前，
    无法验证的在最后。
    """
    rows = []
    for rec in (coverage_payload or {}).get("report", []):
        domain = rec.get("domain")
        if not domain:
            continue
        channel = rec.get("channel", "")
        counts = site_counts(data_dir, domain)
        exp_by_kind = _expected_by_kind(rec)
        quantified = False
        for kind in KINDS:
            exp = exp_by_kind.get(kind)
            if not exp:
                continue
            got = counts[kind]
            missing = exp - got
            if missing <= 0:
                continue
            rows.append({
                "domain": domain, "kind": kind, "got": got, "expected": exp,
                "missing": missing, "ratio": round(got / exp, 3),
                "channel": channel, "verdict": _verdict(channel), "sampled": "",
            })
            quantified = True
        if not exp_by_kind and not quantified:
            rows.append({
                "domain": domain, "kind": "posts", "got": counts["posts"],
                "expected": "", "missing": "", "ratio": "",
                "channel": channel, "verdict": "无法验证（无独立总量源）", "sampled": "",
            })
    rows.sort(key=lambda r: (r["missing"] == "", -(r["missing"] or 0)))
    return rows


# ---------------------------------------------------------------- export
def export_csvs(domains, data_dir, csv_dir) -> dict:
    """复用 export_csv.export 把 jsonl 写成 output/csv/<domain>[.pages|.media].csv。"""
    import export_csv
    out: dict[str, dict] = {}
    csv_dir = Path(csv_dir)
    csv_dir.mkdir(parents=True, exist_ok=True)
    for domain in domains:
        d = Path(data_dir) / domain
        for kind, suffix in (("posts", ""), ("pages", ".pages"), ("media", ".media")):
            jsonl = d / ("articles.jsonl" if kind == "posts" else f"{kind}.jsonl")
            if not jsonl.exists():
                continue
            n = export_csv.export(str(d), str(csv_dir / (domain + suffix + ".csv")), kind)
            out.setdefault(domain, {})[kind] = n
    return out


# ---------------------------------------------------------------- 交付说明
def render_readme(date: str, manifest, residuals, coverage_payload, coverage_path,
                  csv_counts, warning=None) -> str:
    totals = {k: sum(r[k] for r in manifest) for k in KINDS}
    grand = sum(totals.values())
    by_status: dict[str, int] = {}
    for r in manifest:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    quantified = [r for r in residuals if r["missing"] != ""]
    unverified = len(residuals) - len(quantified)
    missing_total = sum(int(r["missing"]) for r in quantified)
    top_res = sorted(quantified, key=lambda r: -int(r["missing"]))[:8]
    top_sites = sorted(manifest, key=lambda r: -r["total"])[:5]
    verified = (coverage_payload or {}).get("verified")
    complete = (coverage_payload or {}).get("complete")
    csv_file_count = sum(len(v) for v in csv_counts.values())
    nice_date = f"{date[:4]}-{date[4:6]}-{date[6:]}"

    lines = [
        "# 南太平洋信源采集数据 · 交付说明",
        "",
        f"- **交付日期**：{nice_date}",
        "- **生成方式**：`collectorctl deliverables`（`collector/deliver.py` 自动生成）",
        f"- **数据目录**：`output/csv/`（CSV 交付物）、`output/delivery/{date}/`（清单与报告）",
        f"- **覆盖率快照**：{('`' + str(coverage_path) + '`') if coverage_path else '无（先跑 `collectorctl verify`）'}",
        "",
        "## 1. 交付物清单",
        "",
        "| 文件 | 说明 |",
        "|---|---|",
        "| `output/csv/<domain>.csv` | 每站 posts，31 列统一 schema（UTF-8 BOM / CRLF / 带引号） |",
        "| `output/csv/<domain>.pages.csv` | 每站 pages，同 31 列 |",
        "| `output/csv/<domain>.media.csv` | 每站 media，12 列 |",
        f"| `output/delivery/{date}/site_manifest.csv` | {len(manifest)} 站清单：行数、覆盖率、缺量、状态、备注、对应文件 |",
        f"| `output/delivery/{date}/residuals.csv` | 残差清单 {len(residuals)} 条"
        f"（{len(quantified)} 条可量化 + {unverified} 条无独立总量源） |",
        f"| `output/delivery/{date}/交付说明.md` | 本文件 |",
        f"| `output/delivery/{date}/coverage.json` | 本次覆盖率快照（归档） |",
        "",
        f"CSV 文件数 {csv_file_count}。",
        "",
        "## 2. 数据规模",
        "",
        "| 类型 | 行数 |",
        "|---|---|",
        f"| posts | {totals['posts']:,} |",
        f"| pages | {totals['pages']:,} |",
        f"| media | {totals['media']:,} |",
        f"| **合计** | **{grand:,}** |",
        "",
    ]
    if top_sites:
        lines += ["Top 5 站点：" + "、".join(f"{r['domain']} {r['total']:,}" for r in top_sites), ""]
    lines += [
        "## 3. 覆盖率与残差",
        "",
        f"- 覆盖率快照：可验证 {verified if verified is not None else '?'} 站，"
        f"其中 ≥95% {complete if complete is not None else '?'} 站。",
        f"- 残差 {len(residuals)} 条，可量化 {len(quantified)} 条，合计缺 {missing_total:,} 行。",
        "- 残差来自与源站独立总量（X-WP-Total / sitemap）的口径差，"
        "自动清单只表示差额，不代表未抓取；具体结论需按 residuals.csv 人工抽样确认。",
        "",
    ]
    if top_res:
        lines += ["| domain | kind | got | expected | missing | ratio |",
                  "|---|---|---:|---:|---:|---:|"]
        lines += [f"| {r['domain']} | {r['kind']} | {int(r['got']):,} | {int(r['expected']):,} "
                  f"| {int(r['missing']):,} | {r['ratio']} |" for r in top_res]
        lines.append("")
    lines += [
        "## 4. 站点状态分布",
        "",
        "| 状态 | 站数 |",
        "|---|---|",
    ]
    for status in ("完成", "有残差", "无法验证", "无数据", "暂停"):
        if status in by_status:
            lines.append(f"| {status} | {by_status[status]} |")
    lines += [
        "",
        "## 5. 复现 / 增量",
        "",
        "```bash",
        "cd " + str(REPO),
        "collectorctl status                       # 看采集状态",
        f"collectorctl deliverables --date {date}    # 重新生成本目录",
        "collectorctl verify                       # 覆盖率核对（写 data/coverage-<date>.json）",
        "collectorctl reconcile --dry-run          # 队列对账（只统计）",
        "collectorctl dedupe --all --dry-run       # 去重（只统计）",
        "```",
        "",
        "## 6. 备注",
        "",
        "- 本文件由 `collector/deliver.py` 自动生成，站点清单/残差为机器口径；人工抽样结论需另行补充。",
    ]
    if warning:
        lines.append(f"- ⚠ {warning}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- run
def _validate_date(date: str) -> str:
    date = (date or "").strip()
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError:
        raise SystemExit(f"--date 需要 YYYYMMDD（例如 20261107），收到：{date!r}")
    return date


def run(date=None, no_export=False, dry_run=False, data_dir=None, csv_dir=None,
        delivery_root=None, sources=None) -> dict:
    """生成交付物。返回汇总 dict；dry_run=True 时只统计不写盘。"""
    date = _validate_date(date or datetime.now().strftime("%Y%m%d"))
    data = Path(data_dir) if data_dir else _default_data_dir()
    csv_out = Path(csv_dir) if csv_dir else CSV_DIR
    delivery_base = Path(delivery_root) if delivery_root else DELIVERY_ROOT
    delivery_dir = delivery_base / date

    src = load_sources(sources)
    skip_keys = read_skip_keys(data)
    coverage, coverage_path, warning = load_coverage(data, date)
    if warning:
        print(f"⚠ {warning}", file=sys.stderr)
    if coverage_path is None:
        print("⚠ 未找到 data/coverage-*.json：残差表为空。先跑 "
              "`collectorctl verify`（或 collector/verify_coverage.py）生成覆盖率快照。",
              file=sys.stderr)

    configured = [s["domain"] for s in src if (s or {}).get("domain")]
    domains = configured + [d for d in site_dirs(data) if d not in set(configured)]

    if dry_run:
        manifest = build_manifest(src, data, coverage, csv_out, skip_keys)
        residuals = build_residuals(coverage, data)
        would_export = {d: site_counts(data, d) for d in domains if any(site_counts(data, d).values())}
        print(f"[dry-run] 将写入：{delivery_dir}/"
              "{site_manifest.csv, residuals.csv, 交付说明.md, coverage.json}")
        print(f"[dry-run] 站点 {len(manifest)} 个，残差 {len(residuals)} 条，"
              f"CSV 待导出站点 {len(would_export)} 个"
              f"{'（--no-export）' if no_export else ''}")
        return {"date": date, "delivery_dir": str(delivery_dir), "dry_run": True,
                "manifest_rows": len(manifest), "residual_rows": len(residuals),
                "csv_exported": {}, "coverage_source": str(coverage_path) if coverage_path else None}

    csv_counts = {} if no_export else export_csvs(domains, data, csv_out)
    manifest = build_manifest(src, data, coverage, csv_out, skip_keys)
    residuals = build_residuals(coverage, data)
    readme = render_readme(date, manifest, residuals, coverage, coverage_path, csv_counts, warning)

    _write_csv(delivery_dir / "site_manifest.csv", MANIFEST_COLS, manifest)
    _write_csv(delivery_dir / "residuals.csv", RESIDUAL_COLS, residuals)
    _write_text_atomic(delivery_dir / "交付说明.md", readme)
    coverage_payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "delivery_date": date,
        "source": str(coverage_path) if coverage_path else None,
        "verified": (coverage or {}).get("verified"),
        "complete": (coverage or {}).get("complete"),
        "report": (coverage or {}).get("report", []),
    }
    _write_text_atomic(delivery_dir / "coverage.json",
                       json.dumps(coverage_payload, ensure_ascii=False, indent=1))

    totals = {k: sum(r[k] for r in manifest) for k in KINDS}
    print(f"交付目录: {delivery_dir}")
    print(f"站点清单: {len(manifest)} 站 -> site_manifest.csv")
    print(f"残差清单: {len(residuals)} 条 -> residuals.csv")
    print(f"CSV: {sum(len(v) for v in csv_counts.values())} 个文件"
          f"{'（--no-export 跳过）' if no_export else ''}"
          f" -> {csv_out}")
    print(f"覆盖率快照: {coverage_path if coverage_path else '无'}")
    print(f"数据规模: posts={totals['posts']:,} pages={totals['pages']:,} "
          f"media={totals['media']:,} 合计={sum(totals.values()):,}")
    return {"date": date, "delivery_dir": str(delivery_dir), "dry_run": False,
            "manifest_rows": len(manifest), "residual_rows": len(residuals),
            "csv_exported": csv_counts,
            "coverage_source": str(coverage_path) if coverage_path else None,
            "totals": totals}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="deliver.py",
        description="生成 output/csv + output/delivery/<yyyymmdd>/{site_manifest,residuals,交付说明,coverage}")
    ap.add_argument("--date", metavar="YYYYMMDD",
                    help="交付日期，默认今天；同时用于选择 data/coverage-<date>.json")
    ap.add_argument("--no-export", action="store_true", help="跳过 output/csv 导出，只出清单")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写任何文件")
    args = ap.parse_args(argv)
    run(date=args.date, no_export=args.no_export, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
