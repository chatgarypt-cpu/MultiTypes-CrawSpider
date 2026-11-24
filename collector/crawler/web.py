"""Shared helpers."""
import html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlsplit

import requests, trafilatura
try:
    from curl_cffi import requests as cffi_requests
except Exception:
    cffi_requests = None
from crawler.config import ROOT, DATA, DB, UA, COLS, MEDIA_COLS, WORKERS
from crawler.public_urls import normalize_url, public_get

# ---------- shared connection pool ----------
# 全局 Session：所有直连请求（get_text / get_many / stream_many / _get / wp_json / cloud_get）
# 复用同一条连接池。无连接池时建连开销占大头（实测复用快 2.1x）。
POOL_CONNECTIONS = 16
POOL_MAXSIZE = 32
CONNECT_TIMEOUT = 10          # 秒；连上服务器的上限
READ_TIMEOUT = 25             # 秒；单次读的上限（body 另受 BODY_DEADLINE 总时延限制）
RETRIES = 2                   # 连接错误 / 5xx 最多重试 2 次（总尝试 3 次）
RETRY_BACKOFF = float(os.environ.get("RETRY_BACKOFF", "0.5"))

BRIGHTDATA_API = "https://api.brightdata.com/request"


def brightdata_get(url, zone=None, timeout=90):
    """Bright Data Unlocker：走对方 IP/指纹池过 Cloudflare/SiteGround，返回 (status, body, {})。

    Key 只从环境变量读（BRIGHTDATA_API_KEY），不写进 sources.yaml。
    """
    key = os.environ.get("BRIGHTDATA_API_KEY") or os.environ.get("API_TOKEN")
    zone = zone or os.environ.get("BRIGHTDATA_ZONE") or "cli_unlocker"
    if not key:
        return 0, "missing BRIGHTDATA_API_KEY", {}
    try:
        r = requests.post(BRIGHTDATA_API, timeout=timeout,
                          data=json.dumps({"zone": zone, "url": url, "format": "raw"}).encode(),
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"})
    except Exception as e:
        return 0, str(e)[:200], {}
    if r.status_code != 200:
        return r.status_code, r.text[:500], {}
    return 200, r.text, {}


SESSION = requests.Session()
# HTTPAdapter(pool_connections=16, pool_maxsize=32)：16 条常驻连接、最多 32 条复用
ADAPTER = requests.adapters.HTTPAdapter(pool_connections=POOL_CONNECTIONS,
                                        pool_maxsize=POOL_MAXSIZE)
SESSION.mount("http://", ADAPTER)
SESSION.mount("https://", ADAPTER)


def _as_timeout(timeout):
    """int/float -> (connect, read)；connect 固定 10s，read 不超过 25s。元组原样返回。"""
    if isinstance(timeout, (tuple, list)) and len(timeout) == 2:
        return tuple(timeout)
    read = float(timeout) if timeout else READ_TIMEOUT
    return (CONNECT_TIMEOUT, min(read, READ_TIMEOUT))


def _sleep_backoff(attempt):
    time.sleep(RETRY_BACKOFF * (2 ** attempt))


def _bb_key():
    env = os.path.join(ROOT, "..", ".env.local")
    for line in open(env):
        if line.startswith("BROWSERBASE_API_KEY="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("BROWSERBASE_API_KEY", "")

def cloud_get(url, timeout=90):
    """Browserbase /v1/fetch: clean egress IP, ~$1 per 1000 pages. Goes through the shared pool."""
    try:
        r = SESSION.post("https://api.browserbase.com/v1/fetch",
                         headers={"X-BB-API-Key": _bb_key(), "Content-Type": "application/json"},
                         json={"url": url}, timeout=timeout)
        d = r.json()
        return d.get("statusCode", 0), d.get("content") or ""
    except Exception as e:
        return 0, str(e)[:150]

from crawler.politeness import DomainLimiter, PUBLIC_ROBOTS, RobotsCache


def site_ua(site):
    return site.get("user_agent") or UA


def headers_for(site):
    """UA + optional replay cookie for sites whose Cloudflare clearance we minted."""
    h = {"User-Agent": site.get("user_agent") or UA, "Accept": "*/*"}
    cf = site.get("cookie_file")
    if cf:
        try:
            data = json.load(open(cf))
            if data.get("cookie"):
                h["Cookie"] = data["cookie"]
        except Exception:
            pass
    return h

LIMITER = DomainLimiter()
ROBOTS = RobotsCache(session=SESSION)     # robots.txt 走同一条连接池

# 有些响应会慢速滴流式返回（或正文几个 GB），单靠 socket timeout 永远读不完，
# 会把整个批次卡死；这里给每个响应加总时延 + 体积上限，并跳过非文本类型。
TEXTUAL = ("text/", "application/xhtml", "application/xml", "application/json", "application/rss")
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", "8000000"))
BODY_DEADLINE = float(os.environ.get("BODY_DEADLINE", "20"))


def _textual(ctype: str | None) -> bool:
    ctype = (ctype or "").lower()
    return not ctype or ctype.startswith(TEXTUAL)


def read_body_capped(r, max_bytes=None, deadline=None, binary=False):
    """读响应体，最多 max_bytes / deadline 秒，超了就截断。"""
    max_bytes = MAX_BODY_BYTES if max_bytes is None else int(max_bytes)
    deadline = BODY_DEADLINE if deadline is None else float(deadline)
    end = time.monotonic() + deadline
    chunks, size = [], 0
    try:
        for ch in r.iter_content(65536):
            if not ch:
                continue
            chunks.append(ch)
            size += len(ch)
            if binary and (size > max_bytes or time.monotonic() > end):
                raise ValueError("PDF exceeds response size/time limit")
            if not binary and (size >= max_bytes or time.monotonic() > end):
                break
    except Exception:
        if binary:
            raise
        pass
    data = b"".join(chunks)[:max_bytes]
    if binary:
        return data
    enc = getattr(r, "encoding", None) or "utf-8"
    try:
        return data.decode(enc, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


_REQ_SEQ = 0


def _reset_session():
    """清理当前会话的 cookie 和连接池。"""
    global SESSION
    try:
        SESSION.close()
    except Exception:
        pass
    SESSION = requests.Session()
    SESSION.mount("http://", ADAPTER)
    SESSION.mount("https://", ADAPTER)


def _rotate_session():
    global _REQ_SEQ
    _REQ_SEQ += 1
    if _REQ_SEQ % 500 == 0:
        _reset_session()


def get_text(url, headers, timeout=(10, 25), verify=True, documents=False, public_host=None,
             proxies=None):
    """流式取页面：先看 content-type，非文本直接放弃；正文有体积/时延上限。

    连接错误 / 5xx 最多重试 2 次（指数退避）；400 清 cookie 重试一次；
    403/429 不重试，留给 politeness 惩罚。
    """
    timeout = _as_timeout(timeout)
    last = (0, "")
    for attempt in range(RETRIES + 1):
        _rotate_session()
        try:
            kwargs = dict(headers=headers, timeout=timeout, stream=True, verify=verify)
            if proxies:
                kwargs["proxies"] = proxies
            if public_host:
                r = public_get(SESSION.get, url, same_host=public_host,
                               allow_target=PUBLIC_ROBOTS.allowed, **kwargs)
            else:
                r = SESSION.get(url, **kwargs)
        except PermissionError:
            return -1, "robots"
        except ValueError as e:
            return 0, str(e)
        except Exception as e:
            last = (0, str(e)[:200])
            if attempt < RETRIES:
                _sleep_backoff(attempt)
                continue
            return last
        try:
            if documents and r.status_code == 200 and r.headers.get("content-type", "").lower().startswith("application/pdf"):
                return r.status_code, read_body_capped(r, binary=True)
            if not _textual(r.headers.get("content-type")):
                return r.status_code, ""
            if (r.status_code >= 500 or r.status_code == 400) and attempt < RETRIES:
                if r.status_code == 400:
                    _reset_session()
                last = (r.status_code, "")
                _sleep_backoff(attempt)
                continue
            return r.status_code, read_body_capped(r)
        finally:
            r.close()


class Fetcher:
    def __init__(self, site):
        self.site = site
        self.browser = site.get("via") == "browser"
        self.ego = site.get("via") == "ego"
        self.cloud = site.get("via") == "cloud"
        self.impersonate = site.get("via") == "impersonate"
        self.playwright = site.get("via") == "playwright"
        self.brightdata = site.get("via") == "brightdata"
        self.camoufox = site.get("via") == "camoufox"
        self.archive = site.get("via") == "archive"
        # 代理池：via: proxy，或站点给了 proxy_pool / proxy 单项
        self.pool = None
        if site.get("via") == "proxy" or site.get("proxy_pool") or site.get("proxy"):
            from crawler import proxies as proxy_mod
            src = site.get("proxy_pool") or ([site["proxy"]] if site.get("proxy") else None)
            self.pool = proxy_mod.pool_for(src, sticky=site.get("proxy_sticky", True))
        # 默认遵守 robots；站点显式写 robots: false 才跳过（用户决定，记在 sources.yaml note 里）
        self.robots = site.get("robots", True) is not False

    def get_many(self, urls, timeout=60):
        if not urls: return []
        if self.ego:
            from crawler.browser_ego import fetch_many as ego_fetch
            json_mode = all(("wp-json" in u or "rest_route" in u or u.endswith(".json")) for u in urls)
            return ego_fetch(urls, json_mode=json_mode,
                             wait_ms=self.site.get("ego_wait_ms"),
                             space="collector-" + self.site["domain"].replace(".", "-"))
        if self.camoufox:
            from crawler.browser_camoufox import fetch_many as cf_fetch
            res = cf_fetch(list(urls),
                           wait_ms=self.site.get("camoufox_wait_ms") or 8000,
                           headless=self.site.get("headless", True),
                           ignore_https_errors=bool(self.site.get("ignore_https_errors")))
            bad = [u for u, st, body in res if st == 0 or not body]
            if bad:
                again = {u: (st, body) for u, st, body in cf_fetch(bad, wait_ms=8000,
                           headless=self.site.get("headless", True),
                           ignore_https_errors=bool(self.site.get("ignore_https_errors")))}
                res = [(u, *(again.get(u) or (st, body))) for u, st, body in res]
            return res
        if self.brightdata:
            zone = self.site.get("brightdata_zone")
            with ThreadPoolExecutor(max_workers=4) as ex:
                return list(ex.map(lambda u: (u, *brightdata_get(u, zone)[:2]), urls))
        if self.playwright:
            from crawler.browser_playwright import fetch_many as pw_fetch
            warm = []
            if not getattr(self, "_warmed", False):
                # 先用首页过一次挑战，拿到的 cookie 后面复用；否则首个列表页可能仍是挑战页
                self._warmed = True
                warm = [self.site["base"].rstrip("/") + "/"]
            res = pw_fetch(warm + list(urls),
                           wait_ms=self.site.get("playwright_wait_ms") or self.site.get("ego_wait_ms"),
                           headless=self.site.get("headless", True),
                           block_resources=bool(self.site.get("block_resources")),
                           ignore_https_errors=bool(self.site.get("ignore_https_errors")),
                           user_agent=site_ua(self.site))
            return res[len(warm):]
        if self.browser:
            return self._browser(urls)
        if self.cloud:
            return [(u, st, body) for u in urls for st, body in [cloud_get(u)]]
        return [self._fetch_one(u, timeout) for u in urls]

    def _fetch_one(self, u, timeout=60):
        public_host = self.site.get("_public_host")
        if public_host:
            try:
                u_checked = normalize_url(u)
                if urlsplit(u_checked).netloc != public_host:
                    return u, 0, "网址不在站点范围内"
            except ValueError as exc:
                return u, 0, str(exc)
        if self.archive:
            # 逆向采集：正文改从公开存档取（源站已下线/按 IP 拦时唯一的取文路径）
            from crawler import paywall as paywall_mod
            st, body, source = paywall_mod.archive_fetch(u)
            LIMITER.acquire(u)
            (LIMITER.penalize if st in (403, 429) else LIMITER.reward)(u)
            return (u, st, body) if source else (u, 0, f"archive_miss {source}")
        robots = PUBLIC_ROBOTS if public_host else ROBOTS
        if self.robots and not robots.allowed(u):
            return u, -1, "robots"
        LIMITER.acquire(u, min_interval=robots.crawl_delay(u) if self.robots else None)
        proxy = self.pool.pick(u) if self.pool else None
        proxy_map = {"http": proxy, "https": proxy} if proxy else None
        try:
            if self.impersonate:
                if cffi_requests is None:
                    raise RuntimeError("via: impersonate requires curl_cffi")
                if proxy_map:
                    st, body = self._impersonate_text(u, headers_for(self.site), proxy_map)
                else:
                    st, body = self._impersonate_text(u, headers_for(self.site))
            else:
                options = {"verify": False} if self.site.get("ignore_https_errors") else {}
                if self.site.get("documents"):
                    options["documents"] = True
                if public_host:
                    options["public_host"] = public_host
                extra = {"proxies": proxy_map} if proxy_map else {}
                st, body = get_text(u, headers=headers_for(self.site), timeout=_as_timeout(timeout),
                                    **extra, **options)
            (LIMITER.penalize if st in (403, 429) else LIMITER.reward)(u)
            if self.pool:
                self.pool.report(proxy, ok=(st == 200))
            return u, st, body
        except Exception as e:
            if self.pool:
                self.pool.report(proxy, ok=False)
            return u, 0, str(e)[:150]

    def _impersonate_text(self, url, headers, proxy_map=None):
        """curl_cffi 浏览器指纹请求；同样重试连接错误/5xx。"""
        last = (0, "")
        for attempt in range(RETRIES + 1):
            try:
                options = {}
                if proxy_map:
                    options["proxies"] = proxy_map
                if self.site.get("ignore_https_errors"):
                    options["verify"] = False
                if self.site.get("http_version") == "1.1":
                    options["http_version"] = 2
                r = cffi_requests.get(url, impersonate="chrome", timeout=45, headers=headers, **options)
                if self.site.get("documents") and r.status_code == 200 and r.headers.get("content-type", "").lower().startswith("application/pdf"):
                    if len(r.content) > MAX_BODY_BYTES:
                        raise ValueError("PDF exceeds response size limit")
                    return r.status_code, r.content
                if r.status_code >= 500 and attempt < RETRIES:
                    last = (r.status_code, "")
                    _sleep_backoff(attempt)
                    continue
                return r.status_code, r.text
            except Exception as e:
                last = (0, str(e)[:200])
                if attempt < RETRIES:
                    _sleep_backoff(attempt)
        return last

    def stream_many(self, urls, workers=4):
        """Yield (url, status, body) with bounded in-site concurrency (non-WP article fetches)."""
        if self.ego or self.browser or self.cloud or self.playwright or self.brightdata or self.camoufox or workers <= 1:
            yield from self.get_many(urls)
            return
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(self._fetch_one, u) for u in urls]
            for f in as_completed(futures):
                yield f.result()

    def stream_requests(self, requests_, workers=4):
        """Yield (Request, status, body) for a batch of Request objects."""
        index = {r.url: r for r in requests_}
        for u, status, body in self.stream_many([r.url for r in requests_], workers=workers):
            yield index.get(u), status, body

    def _browser(self, urls, chunk=40):
        results = []
        for i in range(0, len(urls), chunk):
            part = urls[i:i+chunk]
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
                f.write("\n".join(part)); inp = f.name
            outp = inp + ".ndjson"
            subprocess.run(["node", os.path.join(ROOT, "bb_batch.mjs"), self.site["base"] + "/", inp, outp],
                           check=True, capture_output=True, text=True)
            results += [(r["url"], r["status"], r.get("text", "")) for r in
                        (json.loads(l) for l in open(outp) if l.strip())]
            os.unlink(inp); os.unlink(outp)
        return results

# ---------- discovery ----------
def parse_json_tolerant(body):
    """解析 REST 响应；有些站会在 JSON 前面打 PHP 警告/HTML 片段（Divi、cleanlogin 等）。"""
    if not isinstance(body, (str, bytes, bytearray)):
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        if not isinstance(body, str):
            return None
    idx = [i for i in (body.find("["), body.find("{")) if i >= 0]
    if not idx:
        return None
    try:
        return json.JSONDecoder().raw_decode(body[min(idx):])[0]
    except json.JSONDecodeError:
        return None


def wp_json(urls, fetcher):
    posts = []
    for u, status, body in fetcher.get_many(urls, timeout=90):
        if status != 200:
            yield u, status, None
            continue
        data = parse_json_tolerant(body)
        if data is None:
            yield u, status, None; continue
        if isinstance(data, list): posts += data
    yield None, 200, posts


def _get(session, url, timeout=90, attempts=3, impersonate=False, headers=None):
    """GET with retries on connection errors / 5xx.

    `session` is kept for backwards compatibility: every request goes through the
    module-level pooled SESSION so connections are reused.
    """
    try:
        LIMITER.acquire(url)
    except Exception:
        pass
    headers = headers or {"User-Agent": UA, "Accept": "*/*"}
    last = (0, "", {})
    for i in range(attempts):
        try:
            if impersonate and cffi_requests is not None:
                r = cffi_requests.get(url, impersonate="chrome", timeout=READ_TIMEOUT, headers=headers)
                return r.status_code, r.text, {k.lower(): v for k, v in r.headers.items()}
            r = SESSION.get(url, headers=headers, timeout=_as_timeout(timeout), stream=True)
            try:
                hdrs = {k.lower(): v for k, v in r.headers.items()}
                if r.status_code >= 500 and i < attempts - 1:
                    last = (r.status_code, "", hdrs)
                    _sleep_backoff(i)
                    continue
                body = read_body_capped(r) if _textual(r.headers.get("content-type")) else ""
                return r.status_code, body, hdrs
            finally:
                r.close()
        except Exception as e:
            last = (0, str(e)[:150], {})
            if i < attempts - 1:
                _sleep_backoff(i)
    return last[0], last[1], {}
