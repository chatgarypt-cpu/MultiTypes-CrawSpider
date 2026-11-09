"""Playwright headless Chromium egress — 服务器部署用的浏览器路径（替代 macOS 专属的 ego）。

设计：
- 可选依赖：`uv sync --extra browser` 才会安装 playwright；未安装时 Fetcher 走 via: playwright
  会给出明确错误，不影响其它出口。
- 浏览器二进制：默认使用 Playwright 自己管理的 Chromium（服务器安装：
  `python -m playwright install --with-deps chromium`）。本地/CI 可用
  `PLAYWRIGHT_EXECUTABLE=/path/to/chrome` 指向已有 Chromium（例如 ms-playwright 缓存）。
- 资源拦截：block_resources=True 时 abort image/media/font/stylesheet，用于验证
  "route.abort 省带宽/提速" 的收益（会牺牲 CSS 渲染效果，正文抽取不受影响）。

与 ego 的差异：ego 是 macOS 上的常驻 Chromium app（CDP 脚本驱动）；Playwright 是跨平台的
库 + headless Chromium，适合无 GUI 的 Linux 服务器。迁移路径见 docs/research/ab-20261109.md。
"""
import os
import time

BLOCK_TYPES = {"image", "media", "font", "stylesheet"}
STEALTH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled", "--lang=en-US"]
# 基础反自动化标记（不是完整 stealth；CF Turnstile 仍建议 Patchright）
STEALTH_JS = ("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
              "window.chrome=window.chrome||{runtime:{}};"
              "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});")


def available():
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _executable():
    return os.environ.get("PLAYWRIGHT_EXECUTABLE") or None


def fetch_many(urls, wait_ms=None, headless=True, block_resources=False,
               timeout=None, user_agent=None):
    """抓取一批 URL，返回 [(url, status, html)]；浏览器复用同一个实例。

    单个 URL 失败只返回 status=0 + 错误文本，不中断整批（与 browser_ego 行为一致）。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - 依赖缺失时的明确提示
        raise RuntimeError(
            "playwright 未安装：`uv sync --extra browser`（或 pip install playwright），"
            "并执行 `python -m playwright install --with-deps chromium`"
        ) from exc

    wait_ms = int(wait_ms or os.environ.get("PW_WAIT_MS", "1500"))
    timeout = int(timeout or os.environ.get("PW_TIMEOUT_MS", "30000"))
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, executable_path=_executable(),
                                    args=STEALTH_ARGS)
        context = browser.new_context(user_agent=user_agent) if user_agent else browser.new_context()
        context.add_init_script(STEALTH_JS)
        if block_resources:
            context.route("**/*", lambda route: (
                route.abort() if route.request.resource_type in BLOCK_TYPES else route.continue_()))
        page = context.new_page()
        for u in urls:
            try:
                resp = page.goto(u, wait_until="domcontentloaded", timeout=timeout)
                page.wait_for_timeout(wait_ms)
                # 挑战页会在 load 后继续跳转/刷新：content() 可能报"page is navigating"，重试几次
                html = ""
                deadline = time.time() + (timeout / 1000.0) * 0.5
                while time.time() < deadline:
                    try:
                        html = page.content()
                        break
                    except Exception:
                        page.wait_for_timeout(300)
                out.append((u, resp.status if resp is not None else 0, html))
            except Exception as e:
                out.append((u, 0, str(e)[:150]))
        browser.close()
    return out
