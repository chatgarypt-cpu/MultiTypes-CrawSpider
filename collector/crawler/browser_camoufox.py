"""Headless Firefox transport with a reusable browser context.

Reuse the context across pages so cookies and session state survive between
requests. The server image includes the browser and its Python dependencies.
"""
import atexit
import time

CHALLENGE_MARKERS = ("Robot Challenge", "sgcaptcha", "One moment", "Just a moment",
                     "请稍候", "cf-challenge", "Checking your browser")

_CM = None
_BROWSER = None
_CONTEXT = None
_OPTIONS = None


def _session(headless=True, ignore_https_errors=False):
    global _CM, _BROWSER, _CONTEXT, _OPTIONS
    options = (headless, ignore_https_errors)
    if _CONTEXT is not None and _OPTIONS != options:
        close()
    if _CONTEXT is not None:
        return _CONTEXT
    from camoufox.sync_api import Camoufox
    _CM = Camoufox(headless=headless, humanize=True, locale="en-US",
                   i_know_what_im_doing=True)
    _BROWSER = _CM.__enter__()
    _CONTEXT = _BROWSER.new_context(ignore_https_errors=ignore_https_errors)
    _OPTIONS = options
    atexit.register(close)
    return _CONTEXT


def close():
    global _CM, _BROWSER, _CONTEXT
    try:
        if _CM is not None:
            _CM.__exit__(None, None, None)
    except Exception:
        pass
    _CM = _BROWSER = _CONTEXT = None


def _read(page, timeout):
    html = ""
    deadline = time.time() + (timeout / 1000.0) * 0.5
    while time.time() < deadline:
        try:
            html = page.content()
            break
        except Exception:
            page.wait_for_timeout(300)
    return html


def fetch_many(urls, wait_ms=8000, headless=True, timeout=45000, ignore_https_errors=False):
    """Fetch URLs sequentially; return [(url, status, html)].

    202 is normalized to 200 when real HTML is present: SiteGround serves the
    challenge shell with 202 and the real page stays 202 after solving.
    """
    out = []
    from crawler.htmlpage import page_error
    browser = _session(headless=headless, ignore_https_errors=ignore_https_errors)
    page = browser.new_page()
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
                page.wait_for_timeout(int(wait_ms))
                html = _read(page, timeout)
                if page_error(html) == "challenge":
                    deadline = time.time() + 18
                    while time.time() < deadline and page_error(html) == "challenge":
                        page.wait_for_timeout(2000)
                        more = _read(page, timeout)
                        if more:
                            html = more
                # JSON 接口：Firefox 把 JSON 渲染在 <pre> 里，page.content 是 HTML 包装，
                # 采集器需要原始 JSON，所以这里改取 body.innerText。
                if ("wp-json" in u or "rest_route" in u or u.endswith(".json")
                        or "json" in ((resp.headers or {}).get("content-type", "") if resp else "")):
                    try:
                        inner = page.evaluate("() => document.body ? document.body.innerText : ''")
                        if inner.strip().startswith(("{", "[")):
                            html = inner
                    except Exception:
                        pass
                from crawler.htmlpage import page_error
                current = navigation[-1] if navigation else resp
                status = current.status if current is not None else 0
                if page_error(html) == "challenge":
                    status = 403
                elif page_error(html) == "error_page" and status == 200:
                    status = 503
                out.append((u, status, html))
            except Exception as e:
                out.append((u, 0, str(e)[:150]))
    finally:
        try:
            page.close()
        except Exception:
            pass
    return out
