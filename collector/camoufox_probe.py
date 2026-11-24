#!/usr/bin/env python3
"""Camoufox / Patchright 反爬可通行性探针。

只做单页探测：打开 URL、等待挑战脚本执行、报告是否拿到真实页面。
默认 headless，永不打开可见浏览器窗口。

用法：
    .venv/bin/python collector/camoufox_probe.py \
        --url https://example.org/index.php/events/news \
        --url https://example.org/about/our-people \
        --wait 10000

输出：每个 URL 一段人类可读摘要 + 一行 ``RESULT_JSON {...}``，最后汇总表。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# 目标站已知挑战页特征（大小写不敏感）
CHALLENGE_PATTERNS: Tuple[str, ...] = (
    "Robot Challenge",
    "sgcaptcha",
    "One moment, please",
    "Just a moment",
    "cf-challenge",
    "请稍候",
)

# 挑战页停留后额外等待，给 JS 挑战留出自动通过时间（毫秒）
CHALLENGE_GRACE_MS = 12_000
# 同会话重试时的最少等待
RETRY_WAIT_MS = 10_000


def _now() -> float:
    return time.monotonic()


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def detect_challenges(html: str, text: str, title: str = "") -> List[str]:
    blob = f"{html}\n{text}\n{title}".lower()
    return [p for p in CHALLENGE_PATTERNS if p.lower() in blob]


def _snapshot(page: Any, response: Any, started: float, phase: str) -> Dict[str, Any]:
    """读取当前页面状态，任何单项读取失败都不影响其它字段。"""
    html = ""
    text = ""
    title = ""
    status: Optional[int] = None
    error: Optional[str] = None

    try:
        status = response.status if response is not None else None
    except Exception as exc:  # pragma: no cover - 防御性
        error = f"status: {exc}"

    try:
        title = page.title()
    except Exception as exc:
        error = f"{error + '; ' if error else ''}title: {exc}"

    # 挑战页可能在快照间隙自行跳转，content() 会短暂报错；重试几次再判定
    for attempt in range(3):
        try:
            html = page.content() or ""
            break
        except Exception as exc:
            error = f"{error + '; ' if error else ''}content: {exc}"
            if attempt < 2:
                page.wait_for_timeout(300)

    try:
        text = page.inner_text("body") or ""
    except Exception as exc:
        error = f"{error + '; ' if error else ''}body: {exc}"

    challenges = detect_challenges(html, text, title)
    return {
        "phase": phase,
        "status": status,
        "final_url": page.url,
        "title": _collapse(title)[:300],
        "html_len": len(html),
        "text_len": len(text),
        "challenges": challenges,
        "elapsed_s": round(_now() - started, 2),
        "preview": _collapse(text)[:200],
        "read_error": error,
    }


def _settle(page: Any, wait_ms: int, timeout_ms: int) -> None:
    """固定等待 + 尽力等 networkidle（长轮询页面超时属于正常）。"""
    page.wait_for_timeout(wait_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=min(8_000, timeout_ms))
    except Exception:
        pass


def probe_url(page: Any, url: str, timeout_s: float, wait_ms: int) -> Dict[str, Any]:
    """打开一个 URL；若首屏是挑战页，同页面等待并最多重试导航 1 次。"""
    started = _now()
    timeout_ms = int(timeout_s * 1000)
    attempts: List[Dict[str, Any]] = []
    response = None
    nav_error: Optional[str] = None
    retried = False

    try:
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            # 超时/连接中断时页面可能已经有挑战内容，继续快照而不是丢弃
            nav_error = f"goto: {type(exc).__name__}: {exc}"
        _settle(page, wait_ms, timeout_ms)
        snap = _snapshot(page, response, started, "initial")
        attempts.append(snap)

        if snap["challenges"]:
            # Camoufox 通常能自动跑完 JS 挑战，先原地等待
            page.wait_for_timeout(CHALLENGE_GRACE_MS)
            snap = _snapshot(page, response, started, "challenge-grace")
            attempts.append(snap)

            if snap["challenges"]:
                # 仍被拦：同一上下文内重试一次导航
                retried = True
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as exc:
                    nav_error = f"{nav_error + '; ' if nav_error else ''}retry goto: {type(exc).__name__}: {exc}"
                _settle(page, max(wait_ms, RETRY_WAIT_MS), timeout_ms)
                snap = _snapshot(page, response, started, "retry")
                attempts.append(snap)

        final = attempts[-1]
        if final["challenges"]:
            verdict = "challenge"
        elif final["status"] is not None and final["status"] >= 400:
            verdict = "http_error"
        elif final["html_len"] < 500:
            verdict = "thin"
        else:
            verdict = "ok"

        return {
            "url": url,
            "engine": "camoufox",
            "verdict": verdict,
            "retried": retried,
            "nav_error": nav_error,
            "attempts": attempts,
            **{k: final[k] for k in (
                "status", "final_url", "title", "html_len", "text_len",
                "challenges", "elapsed_s", "preview", "read_error",
            )},
        }
    except Exception as exc:
        return {
            "url": url,
            "engine": "camoufox",
            "verdict": "error",
            "retried": retried,
            "nav_error": nav_error,
            "attempts": attempts,
            "status": None,
            "final_url": url,
            "title": "",
            "html_len": 0,
            "text_len": 0,
            "challenges": [],
            "elapsed_s": round(_now() - started, 2),
            "preview": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_camoufox(urls: List[str], timeout_s: float, wait_ms: int) -> List[Dict[str, Any]]:
    import warnings

    from camoufox.sync_api import Camoufox

    # Camoufox 默认对 canvas/WebGL 加噪声以匹配伪造的 OS/GPU，这是反检测特性，不是故障
    warnings.filterwarnings("ignore", message="Canvas and WebGL readback.*")

    results: List[Dict[str, Any]] = []
    # headless=True：绝不弹窗。humanize 只影响鼠标轨迹，不影响无头。
    with Camoufox(headless=True, humanize=True, locale="en-US") as browser:
        context = browser.new_context(ignore_https_errors=True)
        try:
            for index, url in enumerate(urls, 1):
                print(f"\n[{index}/{len(urls)}] {url}", flush=True)
                page = context.new_page()
                try:
                    record = probe_url(page, url, timeout_s, wait_ms)
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                results.append(record)
                _print_record(record)
        finally:
            try:
                context.close()
            except Exception:
                pass
    return results


def run_patchright(urls: List[str], timeout_s: float, wait_ms: int) -> List[Dict[str, Any]]:
    from patchright.sync_api import sync_playwright

    results: List[Dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        try:
            for index, url in enumerate(urls, 1):
                print(f"\n[{index}/{len(urls)}] {url} (patchright)", flush=True)
                page = context.new_page()
                try:
                    record = probe_url(page, url, timeout_s, wait_ms)
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                record["engine"] = "patchright"
                results.append(record)
                _print_record(record)
        finally:
            try:
                context.close()
            except Exception:
                pass
            browser.close()
    return results


def _print_record(record: Dict[str, Any]) -> None:
    print(
        f"  -> {record['verdict']}  status={record['status']}  "
        f"len={record['html_len']}  {record['elapsed_s']}s  "
        f"challenge={record['challenges'] or '-'}",
        flush=True,
    )
    print(f"     title: {record['title'][:120]}", flush=True)
    print(f"     final: {record['final_url']}", flush=True)
    if record.get("nav_error"):
        print(f"     nav_error: {record['nav_error'][:300]}", flush=True)
    if record.get("error"):
        print(f"     error: {record['error'][:300]}", flush=True)
    print(f"     RESULT_JSON {json.dumps(record, ensure_ascii=False)}", flush=True)


def _print_summary(results: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 100)
    print(f"{'URL':<52} {'VERDICT':<10} {'STATUS':<7} {'LEN':>8} {'TIME':>7}  CHALLENGE")
    print("-" * 100)
    for r in results:
        url = r["url"]
        short = url if len(url) <= 50 else url[:47] + "..."
        print(
            f"{short:<52} {r['verdict']:<10} {str(r['status']):<7} "
            f"{r['html_len']:>8} {r['elapsed_s']:>6}s  {','.join(r['challenges']) or '-'}"
        )
    print("=" * 100)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Camoufox/Patchright headless 反爬探针")
    parser.add_argument("--url", action="append", required=True, help="目标 URL，可重复")
    parser.add_argument("--timeout", type=float, default=45.0, help="单次导航超时（秒），默认 45")
    parser.add_argument("--wait", type=int, default=8_000, help="页面打开后固定等待（毫秒），默认 8000")
    parser.add_argument(
        "--engine", choices=("camoufox", "patchright"), default="camoufox",
        help="浏览器引擎，默认 camoufox",
    )
    args = parser.parse_args(argv)

    urls = args.url
    print(f"engine={args.engine} headless=True timeout={args.timeout}s wait={args.wait}ms urls={len(urls)}")

    try:
        if args.engine == "camoufox":
            results = run_camoufox(urls, args.timeout, args.wait)
        else:
            results = run_patchright(urls, args.timeout, args.wait)
    except ImportError as exc:
        print(f"FATAL: 未安装 {args.engine} 或缺少依赖: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"FATAL: 浏览器启动失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    _print_summary(results)
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
