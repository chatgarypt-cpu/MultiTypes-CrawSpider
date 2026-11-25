"""浏览器抓包：打开页面，收 XHR/fetch 响应，落成标准 HAR 1.2 条目。

为什么单独一个模块：`contract.from_har` 只认 HAR，抓包则是唯一需要浏览器的地方。
把两者切开，契约推断就能纯离线跑（测试用内置 HAR 夹具），抓包则作为可选能力降级：

- 可选依赖：缺少 Playwright 时 `available()` 返回 False，
  `capture()` 抛 RuntimeError 并说明怎么装，调用方（web_preview）吞掉即可。
- `available()` 只探测模块能否导入，不启动浏览器、不找二进制——预览面板不该因为
  本机没装 Chromium 就变慢或报错。
- CLI 复用浏览器。网页试采使用独立、仅限同站公开请求的浏览器上下文。
"""
import importlib
import json
import os
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

CAPTURE_TYPES = {"xhr", "fetch"}


def _sync_api():
    try:
        return importlib.import_module("playwright.sync_api")
    except ImportError:
        return None


def available():
    """Check the dependency without starting a browser."""
    return _sync_api() is not None


def _context(headless=True):
    from crawler import browser_playwright
    return browser_playwright._context(headless=headless)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _mime(headers):
    value = ""
    for name, item in (headers or {}).items():
        if str(name).lower() == "content-type":
            value = str(item)
            break
    return value.split(";")[0].strip().lower()


def _interesting(mime, resource):
    """只收 xhr/fetch，或明确是 JSON/文本的响应。"""
    if resource in CAPTURE_TYPES:
        return True
    return "json" in mime or mime.startswith("text/")


def _entry(response, max_body):
    """Playwright response → HAR entry；不需要的响应返回 None。"""
    request = response.request
    resource = str(getattr(request, "resource_type", "") or "").lower()
    headers = dict(response.headers or {})
    mime = _mime(headers)
    if not _interesting(mime, resource):
        return None
    try:
        body = response.text()
    except Exception:
        body = ""
    if not isinstance(body, str):
        body = ""
    size = len(body.encode("utf-8", "ignore"))
    text = body[:int(max_body)]
    return {
        "startedDateTime": _now(),
        "time": 0,
        "_resourceType": resource or "xhr",
        "request": {
            "method": str(getattr(request, "method", "GET") or "GET").upper(),
            "url": str(getattr(request, "url", "") or ""),
            "headers": dict(getattr(request, "headers", {}) or {}),
        },
        "response": {
            "status": int(getattr(response, "status", 0) or 0),
            "headers": headers,
            "content": {"mimeType": mime or "application/json",
                        "size": size,
                        "text": text,
                        "truncated": size > len(text.encode("utf-8", "ignore"))},
        },
    }


def capture(url, wait_ms=3000, click_selector=None, headless=True,
            max_entries=40, max_body=200_000, public_host=None):
    """打开 url 抓一次包，返回 HAR 兼容结构（log.version = "1.2"）。

    wait_ms：页面加载后再等多久（懒加载接口常在这段时间里才发请求）。
    click_selector：可选，先点一下（「加载更多」/标签页）再等半程。
    max_entries / max_body：条数与单条响应体上限，避免一次抓包把内存撑满。
    """
    if _sync_api() is None:
        raise RuntimeError(
            "capture 需要 playwright：uv sync --extra browser "
            "（或 pip install playwright）后执行 python -m playwright install chromium")
    if public_host:
        from crawler.browser_playwright import public_context
        session = public_context(public_host, headless=headless)
    else:
        session = nullcontext(_context(headless=headless))
    entries = []
    limit = max(1, int(max_entries))

    def on_response(response):
        if len(entries) >= limit:
            return
        try:
            entry = _entry(response, max_body)
        except Exception:
            return
        if entry is not None:
            entries.append(entry)

    with session as context:
        page = context.new_page()
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(int(wait_ms))
            if click_selector:
                try:
                    page.click(click_selector, timeout=5_000)
                    page.wait_for_timeout(max(200, int(wait_ms) // 2))
                except Exception:
                    pass
        finally:
            try:
                page.close()
            except Exception:
                pass
    return {"log": {"version": "1.2",
                    "creator": {"name": "rea-crawlspider", "version": "1"},
                    "entries": entries[:limit]}}


def save(har, path):
    """HAR 落盘（UTF-8、缩进 2），返回写入路径；目录不存在会自动建。"""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def default_dir():
    """抓包默认落盘目录（沿用采集数据目录约定，便于事后复查）。"""
    return Path(os.getenv("CRAWL_DATA_DIR") or "data") / "capture"
