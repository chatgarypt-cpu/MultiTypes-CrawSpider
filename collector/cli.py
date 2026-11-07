#!/usr/bin/env python3
"""collectorctl —— 采集器统一入口。

只做编排：具体实现留在各模块，找不到对应模块时给出清晰提示（不复制实现）。

  collectorctl status
  collectorctl deliverables [--date 20261107] [--no-export] [--dry-run]
  collectorctl verify [--json] [--date YYYYMMDD]
  collectorctl dedupe [<domain> ...] [--all] [--dry-run]
  collectorctl reconcile [--domain X] [--dry-run]
  collectorctl check-site <domain>
  collectorctl pause | resume | paused
  collectorctl run

也可以 `python collector/cli.py <command>`。`run` 会用 os.exec 替换为
collector/supervisor.py（守护进程），因此 launchd / cron 直接指向本入口即可。
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # collector/
REPO = HERE.parent
DATA = HERE / "data"
PAUSE = DATA / "CRAWL_PAUSED"
SKIP = DATA / "CRAWL_SKIP"
PY = sys.executable
sys.path.insert(0, str(HERE))                   # 让 status/export_csv/... 可直接 import


def _ok(msg: str = "") -> int:
    if msg:
        print(msg)
    return 0


def _fail(msg: str, code: int = 1) -> int:
    print(f"✗ {msg}", file=sys.stderr)
    return code


def _import(name: str):
    """按需 import，失败时抛出带安装/依赖提示的 ImportError。"""
    try:
        return importlib.import_module(name)
    except Exception as exc:
        raise ImportError(f"{name} 不可用：{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------- status
def cmd_status(args) -> int:
    try:
        status_mod = _import("status")
        state = status_mod.build_status()
    except ImportError as exc:
        return _fail(str(exc), 3)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=1))
        return 0
    print(f"生成时间 {state['generated']}  总行数 {state['total']:,}  "
          f"速率 {state['rate']} 行/秒  采集进程 {state['tasks_running']}  "
          f"队列 {state['queue_running']}  暂停 {PAUSE.exists()}")
    if state.get("running_domains"):
        print("运行中：" + ", ".join(state["running_domains"]))
    if state.get("blocked"):
        print("阻塞：" + ", ".join(state["blocked"]))
    print(f"\n{'domain':<34}{'rows':>10}{'page':>8}  状态")
    for site in state["sites"][:args.top]:
        mark = "done" if site.get("done") else ""
        print(f"{site['domain']:<34}{site['saved']:>10,}{str(site.get('page') or ''):>8}  {mark}")
    if len(state["sites"]) > args.top:
        print(f"... 另有 {len(state['sites']) - args.top} 站（用 --json 或 -n 查看）")
    return 0


# ---------------------------------------------------------------- deliverables
def cmd_deliverables(args) -> int:
    try:
        deliver = _import("deliver")
    except ImportError as exc:
        return _fail(str(exc), 3)
    try:
        deliver.run(date=args.date, no_export=args.no_export, dry_run=args.dry_run)
    except SystemExit as exc:                   # --date 校验失败
        return _fail(str(exc), 2)
    return 0


# ---------------------------------------------------------------- verify
def cmd_verify(args) -> int:
    script = HERE / "verify_coverage.py"
    if not script.exists():
        return _fail(f"缺少 {script}（覆盖率核对模块）", 3)
    proc = subprocess.run([PY, str(script), "--json"], capture_output=True, text=True)
    if proc.returncode != 0:
        return _fail(f"verify_coverage.py 退出码 {proc.returncode}："
                     f"{(proc.stderr or proc.stdout).strip()[-400:]}", proc.returncode)
    text = proc.stdout
    start = text.find("{")
    try:
        payload = json.loads(text[start:]) if start >= 0 else {}
    except ValueError:
        return _fail(f"verify_coverage.py 输出无法解析为 JSON：{text.strip()[:200]}", 3)
    date = args.date or time.strftime("%Y%m%d")
    out = DATA / f"coverage-{date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)
    print(f"覆盖率快照 -> {out}")
    print(f"可验证 {payload.get('verified', '?')} 站，"
          f"其中 ≥95% {payload.get('complete', '?')} 站")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------- dedupe
def cmd_dedupe(args) -> int:
    try:
        dedupe_mod = _import("dedupe")
    except ImportError as exc:
        return _fail(str(exc), 3)
    fn = dedupe_mod.dedupe
    supports_dry_run = "dry_run" in inspect.signature(fn).parameters
    data_dir = Path(getattr(dedupe_mod, "DATA", DATA))
    if args.all:
        domains = sorted({p.parent.name for p in
                          data_dir.glob("*/articles.jsonl") if p.is_file()})
    else:
        domains = args.domains
    if not domains:
        return _fail("需要 <domain> ... 或 --all", 2)
    rc = 0
    for domain in domains:
        try:
            if args.dry_run and not supports_dry_run:
                stats = _dry_run_estimate(data_dir, domain)
            else:
                stats = fn(domain, dry_run=True) if args.dry_run else fn(domain)
        except Exception as exc:
            print(f"✗ {domain}: {type(exc).__name__}: {exc}", file=sys.stderr)
            rc = 1
            continue
        if stats:
            print(json.dumps(stats, ensure_ascii=False))
        else:
            print(f"{domain}: 无 articles.jsonl，跳过")
    return rc


def _dry_run_estimate(data_dir: Path, domain: str) -> dict:
    """旧版 dedupe 没有 dry_run 时的只读估算（绝不写盘）。"""
    from crawler.request import canonical_key
    path = data_dir / domain / "articles.jsonl"
    if not path.exists():
        return {}
    total, keys = 0, set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            total += 1
            keys.add(canonical_key(row.get("url") or ""))
    return {"domain": domain, "before": total, "after": len(keys),
            "removed": total - len(keys), "dry_run": True, "estimated": True}


# ---------------------------------------------------------------- reconcile
def cmd_reconcile(args) -> int:
    try:
        reconcile_mod = _import("crawler.reconcile")
    except ImportError as exc:
        return _fail(
            f"reconcile 尚未就绪（T1 负责新增 crawler/reconcile.py）：{exc}\n"
            "  就绪后本命令自动可用，无需改 CLI。", 3)
    fn = getattr(reconcile_mod, "reconcile", None)
    if fn is None:
        return _fail("crawler/reconcile.py 没有 reconcile(domain=None, dry_run=False)", 3)
    result = fn(domain=args.domain, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------- check-site
def cmd_check_site(args) -> int:
    collect_py = HERE / "collect.py"
    if not collect_py.exists():
        return _fail(f"缺少 {collect_py}", 3)
    proc = subprocess.run([PY, str(collect_py), "--check-site", args.domain],
                          capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        if "unrecognized arguments" in proc.stderr or "--check-site" in proc.stderr:
            return _fail(
                "check-site 尚未就绪（T3 负责 collect.py --check-site）："
                "当前 collect.py 不认 --check-site；就绪后本命令自动可用。", 3)
        return proc.returncode
    return 0


# ---------------------------------------------------------------- pause / resume
def cmd_pause(_args) -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    PAUSE.touch()
    return _ok("已暂停：新建 data/CRAWL_PAUSED（守护进程会在下个循环停止子进程；"
               "`collectorctl resume` 恢复）")


def cmd_resume(_args) -> int:
    try:
        PAUSE.unlink()
        return _ok("已恢复：删除 data/CRAWL_PAUSED")
    except FileNotFoundError:
        return _ok("当前未暂停（data/CRAWL_PAUSED 不存在）")


def cmd_paused(_args) -> int:
    paused = PAUSE.exists()
    print(f"暂停开关 data/CRAWL_PAUSED: {'存在（已暂停）' if paused else '不存在（运行中）'}")
    if SKIP.exists():
        keys = [ln.strip() for ln in SKIP.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
        print(f"按站跳过 data/CRAWL_SKIP: {', '.join(keys) if keys else '空'}")
    state_path = DATA / "supervise_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    if state:
        print(f"守护状态更新于 {state.get('updated')}："
              f"running={len(state.get('running', []))} "
              f"pending={len(state.get('pending', []))} "
              f"done={len(state.get('completed', []))} "
              f"stalled={len(state.get('stalled', []))}")
    else:
        print("守护状态: 无 data/supervise_state.json（守护进程可能未运行）")
    return 0


# ---------------------------------------------------------------- run
def cmd_run(_args) -> int:
    supervisor = HERE / "supervisor.py"
    if not supervisor.exists():
        return _fail(f"缺少 {supervisor}", 3)
    os.execv(PY, [PY, str(supervisor)])         # 替换当前进程，信号直达守护进程
    return 0                                    # 不会执行到这里


# ---------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="collectorctl",
        description="采集器统一入口（编排 status / 交付 / 校验 / 去重 / 对账 / 守护）")
    sub = ap.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("status", help="采集进度总览（复用 collector/status.py）")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    p.add_argument("-n", "--top", type=int, default=20, help="表格显示前 N 站（默认 20）")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("deliverables",
                       help="生成 output/csv + output/delivery/<yyyymmdd>/ 四件套")
    p.add_argument("--date", metavar="YYYYMMDD", help="交付日期，默认今天")
    p.add_argument("--no-export", action="store_true", help="跳过 output/csv 导出")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    p.set_defaults(func=cmd_deliverables)

    p = sub.add_parser("verify", help="跑 verify_coverage.py 并写 data/coverage-<date>.json")
    p.add_argument("--json", action="store_true", help="额外打印完整覆盖率 JSON")
    p.add_argument("--date", metavar="YYYYMMDD", help="快照日期，默认今天")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("dedupe", help="按 canonical url 去重（复用 collector/dedupe.py）")
    p.add_argument("domains", nargs="*", help="站点域名；配合 --all 时可省略")
    p.add_argument("--all", action="store_true", help="处理所有有 articles.jsonl 的站点")
    p.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    p.set_defaults(func=cmd_dedupe)

    p = sub.add_parser("reconcile", help="队列对账：urls.txt 已有的 unprocessed 行标 done（T1）")
    p.add_argument("--domain", help="只处理指定站点")
    p.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("check-site", help="干跑发现阶段，打印 N 条 URL，不入库（T3）")
    p.add_argument("domain", help="站点域名，如 www.palaugov.pw")
    p.set_defaults(func=cmd_check_site)

    for name, func, desc in (("pause", cmd_pause, "暂停采集（新建 data/CRAWL_PAUSED）"),
                             ("resume", cmd_resume, "恢复采集（删除 data/CRAWL_PAUSED）"),
                             ("paused", cmd_paused, "查看暂停开关与守护状态")):
        p = sub.add_parser(name, help=desc)
        p.set_defaults(func=func)

    p = sub.add_parser("run", help="常驻守护（= collector/supervisor.py）")
    p.set_defaults(func=cmd_run)
    return ap


DISPATCH = {
    "status": cmd_status, "deliverables": cmd_deliverables, "verify": cmd_verify,
    "dedupe": cmd_dedupe, "reconcile": cmd_reconcile, "check-site": cmd_check_site,
    "pause": cmd_pause, "resume": cmd_resume, "paused": cmd_paused, "run": cmd_run,
}


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
