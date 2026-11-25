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
import atexit
import glob
import os
import time
from urllib.parse import urlsplit

BLOCK_TYPES = {"image", "media", "font", "stylesheet"}
# 挑战页特征：SiteGround / Cloudflare 的过渡页。命中后等它自动跳转再取一次。
CHALLENGE_MARKERS = ("Robot Challenge", "sgcaptcha", "One moment", "Just a moment",
                     "请稍候", "cf-challenge", "Checking your browser")
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
    """显式 env 优先；本机没配时自动找 ms-playwright 缓存里的 Chrome for Testing。"""
    env = os.environ.get("PLAYWRIGHT_EXECUTABLE")
    if env:
        return env
    home = os.path.expanduser("~")
    for pat in ("Library/Caches/ms-playwright/chromium-*/chrome-mac-arm64/"
                "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                "Library/Caches/ms-playwright/chromium-*/chrome-linux/chrome"):
        hits = sorted(glob.glob(os.path.join(home, pat)))
        if hits:
            return hits[-1]
    return None


_PW = None
_BROWSER = None
_CONTEXT = None
_OPTIONS = None


def _context(headless=True, user_agent=None, block_resources=False, ignore_https_errors=False,
             public_host=None):
    """整个进程复用一个浏览器上下文：挑战通过后的 cookie 能带到后续页面，
    避免每批 URL 重新过 Cloudflare / SiteGround 挑战。"""
    global _PW, _BROWSER, _CONTEXT, _OPTIONS
    options = (headless, user_agent, block_resources, ignore_https_errors, public_host)
    if _CONTEXT is not None and _OPTIONS != options:
        close()
    if _CONTEXT is not None:
        return _CONTEXT
    from playwright.sync_api import sync_playwright
    _PW = sync_playwright().start()
    _BROWSER = _PW.chromium.launch(headless=headless, executable_path=_executable(),
                                   args=[] if public_host else STEALTH_ARGS)
    _CONTEXT = _BROWSER.new_context(ignore_https_errors=ignore_https_errors,
                                  **({"service_workers": "block", "accept_downloads": False} if public_host else {}),
                                  **({"user_agent": user_agent} if user_agent else {}))
    _OPTIONS = options
    if public_host:
        def public_request(route):
            p = urlsplit(route.request.url)
            if (p.scheme in ("http", "https") and p.netloc == public_host
                    and route.request.method in ("GET", "HEAD")):
                response = route.fetch(max_redirects=0)
                try:
                    if 300 <= response.status < 400 and response.headers.get("location"):
                        route.abort()
                    else:
                        route.fulfill(response=response)
                finally:
                    response.dispose()
            else:
                route.abort()
        _CONTEXT.route("**/*", public_request)
        _CONTEXT.route_web_socket("**/*", lambda socket: socket.close())
    else:
        _CONTEXT.add_init_script(STEALTH_JS)
    if block_resources and not public_host:
        _CONTEXT.route("**/*", lambda route: (
            route.abort() if route.request.resource_type in BLOCK_TYPES else route.continue_()))
    atexit.register(close)
    return _CONTEXT


def close():
    global _PW, _BROWSER, _CONTEXT
    try:
        if _CONTEXT is not None:
            _CONTEXT.close()
        if _BROWSER is not None:
            _BROWSER.close()
        if _PW is not None:
            _PW.stop()
    except Exception:
        pass
    _PW = _BROWSER = _CONTEXT = None


def _read_html(page, timeout):
    """取当前 DOM；挑战页跳转期间 content() 会报错，重试到超时一半。"""
    html = ""
    deadline = time.time() + (timeout / 1000.0) * 0.5
    while time.time() < deadline:
        try:
            html = page.content()
            break
        except Exception:
            page.wait_for_timeout(300)
    return html


def fetch_many(urls, *args, **kwargs):
    return list(iter_fetch(urls, *args, **kwargs))


def iter_fetch(urls, wait_ms=None, headless=True, block_resources=False,
               timeout=None, user_agent=None, ignore_https_errors=False, public_host=None):
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
    from crawler.htmlpage import page_error
    context = _context(headless=headless, user_agent=user_agent, block_resources=block_resources,
                       ignore_https_errors=ignore_https_errors, **({"public_host": public_host} if public_host else {}))
    page = context.new_page()
    navigation = []
    def on_response(response):
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            navigation.append(response)
    page.on("response", on_response)
    try:
        for u in urls:
            resp = None
            navigation.clear()
            try:
                resp = page.goto(u, wait_until="domcontentloaded", timeout=timeout)
                page.wait_for_timeout(wait_ms)
                html = _read_html(page, timeout)
                # SiteGround/Cloudflare 挑战页会自动跳转：最多再等 18 秒，直到正文出现
                if not public_host and page_error(html) == "challenge":
                    deadline = time.time() + 18
                    while time.time() < deadline and page_error(html) == "challenge":
                        page.wait_for_timeout(2500)
                        more = _read_html(page, timeout)
                        if more:
                            html = more
                from crawler.htmlpage import page_error
                current = navigation[-1] if navigation else resp
                status = current.status if current is not None else 0
                if page_error(html) == "challenge":
                    status = 403
                elif page_error(html) == "error_page" and status == 200:
                    status = 503
                if "wp-json" in u or "rest_route" in u or u.endswith(".json"):
                    inner = page.evaluate("() => document.body ? document.body.innerText : ''")
                    if inner.strip().startswith(("{", "[")):
                        html = inner
                result = (u, status, html)
            except Exception as e:
                result = (u, 0, str(e)[:150])
            yield result
    finally:
        try:
            page.close()
        except Exception:
            pass
