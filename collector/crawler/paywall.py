"""付费墙 / 反爬取文回退：把 wasi-master/13ft 的多源思路接进采集器。

13ft（https://github.com/wasi-master/13ft）是自托管的 12ft.io 替代，它的取文链路是：

1. 用 Googlebot UA 直取原页面 —— 新闻站为 SEO 会对 Googlebot 放出全量正文；
2. Medium 走 Freedium 镜像（freedium-mirror.cfd / freedium.cfd）；
3. 仍失败 → archive.org 的 wayback 快照，取 `id_` 原始版本（不带存档器改写的横幅）；
4. 仍失败 → archive.today / archive.ph / archive.is 的 `newest/` 快照。

原项目是 Flask + 前端状态页 + 反 frame-busting 脚本清理，采集器不需要这些：这里只
保留"拿到一份能交给 trafilatura 的 HTML"这一段，输出与 `Fetcher` 的 (status, body)
同形，调用方不用改提取逻辑。

默认关闭；站点显式写开关才生效（行为不变，robots 策略不变）：

    - domain: www.example.com
      premium_marker: id="paywall"
      paywall_fallback: 13ft        # 13ft | googlebot | archive | freedium

- `13ft`       Googlebot 直取 → Freedium（仅 medium）→ archive.org → archive.today
- `googlebot`  只走 Googlebot 直取
- `archive`    只走两个公开存档（不伪装 UA）
- `freedium`   只走 Freedium（Medium 专用）
- `13ft-service` 调自建 13ft 服务（`paywall_service:` 或 `PAYWALL_SERVICE_URL`，
  默认端口 9982，接口 `POST /article` form `link=<url>`）

预算：`PAYWALL_FALLBACK_BUDGET`（默认 80）限制每站每轮的额外请求组数（各 channel 入口
`reset_budget()`），避免整站 403 时把 archive.org 打爆；预算耗尽后直接返回未恢复。
"""
import os
import re
from urllib.parse import quote, urlparse

import requests

from crawler.config import UA
from crawler.htmlpage import markdown, page_error

GOOGLEBOT_UA = ("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.119 Mobile Safari/537.36 "
                "(compatible; Googlebot/2.1; +http://www.google.com/bot.html)")
BROWSER_UA = UA
GOOGLEBOT_HEADERS = {"User-Agent": GOOGLEBOT_UA, "Accept": "*/*"}
BROWSER_HEADERS = {"User-Agent": BROWSER_UA,
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Accept-Language": "en-US,en;q=0.9"}

ARCHIVE_TODAY_MIRRORS = ("archive.ph", "archive.today", "archive.is", "archive.li")
# 存档里常见"占位页"：主机默认页、404 页、站点欢迎页。它们字数和正文都像文章，
# 只能按 <title> 识别（tokelau 的 /+government.html 存档就是 304 字的 Page not found）。
PLACEHOLDER_TITLES = ("apache2 ubuntu default page", "welcome to nginx", "index of /",
                      "it works!", "page not found", "404 not found", "not found",
                      "welcome to", "coming soon", "under construction")
FREEDIUM_MIRRORS = ("freedium-mirror.cfd", "freedium.cfd")
WAYBACK_API = "https://archive.org/wayback/available"
MIN_BODY = 500          # 小于这个长度的响应当成空壳/错误页

BUDGET = int(os.environ.get("PAYWALL_FALLBACK_BUDGET", "80"))
MODE_ALIASES = {"13ft": "13ft", "full": "13ft", "googlebot": "googlebot",
                "archive": "archive", "wayback": "archive", "freedium": "freedium",
                "service": "service", "13ft-service": "service"}

SESSION = requests.Session()
_budget = {"left": BUDGET}
_health = {"archive_today_failures": 0}     # 连续全镜像失败次数：整轮里连着挂就不再浪费时间
ARCHIVE_TODAY_MAX_FAILURES = 2


def reset_budget(value=None):
    """每站采集开始时重置尝试预算（channels/* 入口调用，测试也用）。"""
    _budget["left"] = BUDGET if value is None else int(value)


def reset_health():
    """真正开始新一轮时清掉 archive.today 的连续失败计数（测试也用）。"""
    _health["archive_today_failures"] = 0


def _take_budget():
    if _budget["left"] <= 0:
        return False
    _budget["left"] -= 1
    return True


def mode(site):
    return MODE_ALIASES.get(str((site or {}).get("paywall_fallback") or "").strip().lower(), "")


def enabled(site):
    return bool(mode(site))


def is_medium(url):
    host = (urlparse(url).hostname or "").lower()
    return host == "medium.com" or host.endswith(".medium.com")


def is_archived(url):
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("archive.org") or any(host == m or host.endswith("." + m)
                                               for m in ARCHIVE_TODAY_MIRRORS)


def placeholder(body):
    """存档返回的是主机默认页 / 404 页 / 欢迎页（不是文章）时返回命中的标记。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", body or "", re.I | re.S)
    title = " ".join(m.group(1).split()).lower() if m else ""
    return next((p for p in PLACEHOLDER_TITLES if p in title), "")


def _is_html(text):
    head = (text or "")[:400].lower()
    return "<html" in head or "<body" in head or "<!doctype" in head


def usable(body, previous=""):
    """一份响应是否值得交给提取器：非空、长度够、不是挑战/错误页、且正文确实变多了。

    只比字符串不够：同一份被付费墙截断的页面，两次请求只因 nonce/token 不同就会不相等。
    所以和已有正文比 markdown 长度，没有实质增量（< +400 字符且 < 1.2 倍）就当没恢复，
    继续试下一个源 —— 否则会在"Googlebot 拿到的还是摘要"时提前收工。
    """
    if not isinstance(body, str) or len(body) < MIN_BODY:
        return False
    if page_error(body) or placeholder(body):
        return False
    if not previous:
        return True
    if body.strip() == previous.strip():
        return False
    try:
        old_len = len(markdown(previous) or "") if _is_html(previous) else len(previous)
        new_len = len(markdown(body) or "") if _is_html(body) else len(body)
    except Exception:
        return True
    return new_len >= old_len + 400 and new_len >= old_len * 1.2


def _get(url, headers, timeout=(10, 25), allow_redirects=True, verify=True, public_host=None):
    from crawler.web import read_body_capped
    try:
        options = dict(headers=headers, timeout=timeout, verify=verify, stream=True)
        if public_host:
            from crawler.public_urls import public_get
            r = public_get(SESSION.get, url, same_host=public_host, **options)
        else:
            r = SESSION.get(url, allow_redirects=allow_redirects, **options)
        try:
            body = read_body_capped(r, strict=True) if r.status_code == 200 else ""
            return r.status_code, body, r.url or url
        finally:
            r.close()
    except (ValueError, OSError, requests.RequestException):
        return 0, "", ""


def _service(url, base):
    """调用自建 13ft 服务（`docker compose up` 默认 127.0.0.1:9982）的 POST /article。"""
    if not base:
        return 0, "", ""
    try:
        r = SESSION.post(base.rstrip("/") + "/article", data={"link": url},
                         headers=BROWSER_HEADERS, timeout=(10, 45), allow_redirects=True)
    except Exception:
        return 0, "", ""
    if r.status_code != 200:
        r.close()
        return r.status_code, "", ""
    try:
        return 200, r.text or "", r.url or base
    finally:
        r.close()


READER_BASE = os.environ.get("PAYWALL_READER_BASE", "https://r.jina.ai/")


def _reader(url):
    """第三方 Reader 服务取文：从对方的 IP 抓，绕开"按出口 IP 限流"的站。

    返回 Markdown（`Title: ... / Markdown Content:` 结构），交给 extract 的纯文本直通。
    只在链尾兜底，避免把每次回退都变成外部请求。
    """
    if os.environ.get("PAYWALL_READER", "1").strip().lower() in ("0", "false", "off"):
        return 0, "", ""
    try:
        r = requests.get(READER_BASE + url, headers={"Accept": "text/plain"},
                         timeout=(10, 45))
    except Exception:
        return 0, "", ""
    if r.status_code != 200:
        r.close()
        return r.status_code, "", ""
    try:
        text = r.text or ""
    finally:
        r.close()
    if "Markdown Content:" not in text:
        return 0, "", ""
    return 200, text, url


def _bpc(url):
    """按 Bypass Paywalls Clean 规则取文：换 UA / 干净会话 / 伪造来源。

    规则来自上游 sites.js，语义见 crawler/bpc.py 的表；这里只执行"HTTP 层能做的"，
    block_regex（拦截付费墙脚本）留给浏览器路径。
    """
    from crawler import bpc as bpc_mod
    plan = bpc_mod.plan(url)
    if not plan:
        return 0, "", ""
    try:
        session = requests.Session()          # 无 cookie = 上游 remove_cookies 的效果
        r = session.get(url, headers=bpc_mod.headers(url), timeout=(10, 25), allow_redirects=True)
    except Exception:
        return 0, "", ""
    if r.status_code != 200:
        r.close()
        return r.status_code, "", ""
    try:
        r.encoding = r.apparent_encoding or "utf-8"
        return 200, r.text or "", r.url or url
    finally:
        r.close()


def _googlebot(url):
    return _get(url, GOOGLEBOT_HEADERS)


def _freedium(url):
    clean = url.split("?")[0]
    for mirror in FREEDIUM_MIRRORS:
        # 镜像站常见证书错配；只取公开文章，这里不对镜像做证书校验
        status, body, final = _get("https://%s/%s" % (mirror, clean), BROWSER_HEADERS,
                                   verify=False)
        if status != 200 or "ARTICLE_NOT_FOUND" in body:
            continue
        if "main-content" in body or "<main" in body:
            return 200, body, final
    return 0, "", ""


def _archive_org(url, public=False):
    args = {"public_host": "archive.org"} if public else {}
    status, raw, _ = _get(WAYBACK_API + "?url=" + quote(url, safe=""), BROWSER_HEADERS, timeout=15, **args)
    if status != 200:
        return 0, "", ""
    import json
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return 0, "", ""
    snapshots = payload.get("archived_snapshots") if isinstance(payload, dict) else None
    snapshot = snapshots.get("closest") if isinstance(snapshots, dict) else None
    if not isinstance(snapshot, dict) or not snapshot.get("available"):
        return 0, "", ""
    target = snapshot.get("url")
    if not isinstance(target, str) or not target.startswith(("https://", "http://")):
        return 0, "", ""
    if target.startswith("http://"):
        target = "https://" + target[len("http://"):]
    # 取 id_ 原始快照：存档器不加横幅/改写链接，正文更干净
    if "/web/" in target and "/id_/" not in target:
        stamp, _, rest = target.split("/web/", 1)[1].partition("/")
        target = target.replace("/web/" + stamp + "/", "/web/" + stamp + "id_/", 1)
    if public:
        return _get(target, BROWSER_HEADERS, public_host="web.archive.org")
    return _get(target, GOOGLEBOT_HEADERS)


def _archive_today(url):
    if _health["archive_today_failures"] >= ARCHIVE_TODAY_MAX_FAILURES:
        return 0, "", ""
    for mirror in ARCHIVE_TODAY_MIRRORS:
        status, body, final = _get("https://%s/newest/%s" % (mirror, quote(url, safe=":/")),
                                   BROWSER_HEADERS, timeout=(8, 15), verify=False)
        if status != 200:
            continue
        if not re.search(r"archive\.(ph|today|is|li)/[A-Za-z0-9]{4,}", final or ""):
            continue
        if "/newest/" in (final or ""):
            continue
        head = body[:5000].lower()
        if "no results" in head or "0 captures" in head:
            continue
        return 200, body, final
    _health["archive_today_failures"] += 1
    return 0, "", ""


def _candidates(url, site):
    """按 13ft 的顺序给出候选取文方式；每个是 (source, callable -> (status, body, final))。"""
    m = mode(site)
    medium = is_medium(url)
    if m == "service":
        base = site.get("paywall_service") or os.environ.get("PAYWALL_SERVICE_URL", "")
        return [("service", lambda: _service(url, base))] if base else []
    if m == "googlebot":
        return [("googlebot", lambda: _googlebot(url))]
    if m == "archive":
        return [("archive_org", lambda: _archive_org(url)),
                ("archive_today", lambda: _archive_today(url))]
    if m == "freedium":
        return [("freedium", lambda: _freedium(url))] if medium else []
    chain = []
    # BPC 规则最便宜（一次请求，不改 UA 之外的行为），有规则就先试
    try:
        from crawler import bpc as bpc_mod
        has_rule = bool(bpc_mod.match(url)[0])
    except Exception:
        has_rule = False
    if has_rule:
        chain.append(("bpc", lambda: _bpc(url)))
    if not medium:
        chain.append(("googlebot", lambda: _googlebot(url)))
    if medium:
        chain.append(("freedium", lambda: _freedium(url)))
    chain += [("archive_org", lambda: _archive_org(url)),
              ("archive_today", lambda: _archive_today(url)),
              ("reader", lambda: _reader(url))]
    return chain


def recover(url, site, body="", status=0):
    """按 13ft 的顺序取一份可解析 HTML。

    返回 `(status, body, source)`：成功是 `(200, html, "googlebot"|"freedium"|
    "archive_org"|"archive_today")`；没恢复是 `(status, body, "")`，调用方按原逻辑
    记错误/跳过。`body` 是直连已经拿到的内容，用来判重（archive 快照与直连相同就不算恢复）。
    """
    if not enabled(site) or is_archived(url):
        return status, body, ""
    if not _take_budget():
        return status, body, ""
    for source, fetch in _candidates(url, site):
        st, candidate, _final = fetch()
        if st != 200 or not usable(candidate, body if isinstance(body, str) else ""):
            continue
        return 200, candidate, source
    return status, body, ""


def archive_fetch(url, public=False):
    """逆向采集出口（`via: archive`）：只走公开存档，返回 (status, body, source)。

    与 `recover()` 的区别：不看站点开关、不伪装 Googlebot、不消耗回退预算 —— 这是
    "源站已下线，正文只能从存档读"的正常采集路径，而不是失败后的补救。
    """
    if is_archived(url):
        return 0, "", ""
    if public:
        status, body, _ = _archive_org(url, public=True)
        return (status, body, "archive_org") if status == 200 and usable(body) else (0, "", "")
    for source, fetch in (("archive_org", lambda: _archive_org(url)),
                          ("archive_today", lambda: _archive_today(url))):
        status, body, _final = fetch()
        if status == 200 and usable(body):
            return 200, body, source
    return 0, "", ""
