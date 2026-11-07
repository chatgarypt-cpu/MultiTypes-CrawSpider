"""Shared helpers."""
import html, json, logging, math, os, re, sqlite3, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests, trafilatura
try:
    from curl_cffi import requests as cffi_requests
except Exception:
    cffi_requests = None
from crawler.config import ROOT, DATA, DB, UA, COLS, MEDIA_COLS, WORKERS

# ---------- shared connection pool ----------
# 全局 Session：所有直连请求（get_text / get_many / stream_many / _get / wp_json / cloud_get）
# 复用同一条连接池。无连接池时建连开销占大头（实测复用快 2.1x）。
POOL_CONNECTIONS = 16
POOL_MAXSIZE = 32
CONNECT_TIMEOUT = 10          # 秒；连上服务器的上限
READ_TIMEOUT = 25             # 秒；单次读的上限（body 另受 BODY_DEADLINE 总时延限制）
RETRIES = 2                   # 连接错误 / 5xx 最多重试 2 次（总尝试 3 次）
RETRY_BACKOFF = float(os.environ.get("RETRY_BACKOFF", "0.5"))

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

from crawler.politeness import DomainLimiter, RobotsCache


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


def _textual(ctype):
    ctype = (ctype or "").lower()
    return not ctype or ctype.startswith(TEXTUAL)


def read_body_capped(r, max_bytes=None, deadline=None):
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
            if size >= max_bytes or time.monotonic() > end:
                break
    except Exception:
        pass
    data = b"".join(chunks)[:max_bytes]
    enc = getattr(r, "encoding", None) or "utf-8"
    try:
        return data.decode(enc, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


def get_text(url, headers, timeout=(10, 25)):
    """流式取页面：先看 content-type，非文本直接放弃；正文有体积/时延上限。

    连接错误 / 5xx 最多重试 2 次（指数退避）；403/429 不重试，留给 politeness 惩罚。
    """
    timeout = _as_timeout(timeout)
    last = (0, "")
    for attempt in range(RETRIES + 1):
        try:
            r = SESSION.get(url, headers=headers, timeout=timeout, stream=True)
        except Exception as e:
            last = (0, str(e)[:200])
            if attempt < RETRIES:
                _sleep_backoff(attempt)
                continue
            return last
        try:
            if not _textual(r.headers.get("content-type")):
                return r.status_code, ""
            if r.status_code >= 500 and attempt < RETRIES:
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

    def get_many(self, urls, timeout=60):
        if not urls: return []
        if self.ego:
            from crawler.browser_ego import fetch_many as ego_fetch
            json_mode = all(("wp-json" in u or "rest_route" in u or u.endswith(".json")) for u in urls)
            return ego_fetch(urls, json_mode=json_mode, space="collector-" + self.site["domain"].replace(".", "-"))
        if self.browser:
            return self._browser(urls)
        if self.cloud:
            return [(u, st, body) for u in urls for st, body in [cloud_get(u)]]
        headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
        to = _as_timeout(timeout)                 # 连接池 + connect=10/read<=25，保序返回
        return [(u, *get_text(u, headers=headers, timeout=to)) for u in urls]

    def _impersonate_text(self, url, headers):
        """curl_cffi 浏览器指纹请求；同样重试连接错误/5xx。"""
        last = (0, "")
        for attempt in range(RETRIES + 1):
            try:
                r = cffi_requests.get(url, impersonate="chrome", timeout=45, headers=headers)
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
        if self.ego or self.browser or self.cloud or workers <= 1:
            yield from self.get_many(urls)
            return
        headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

        def one(u):
            if not ROBOTS.allowed(u):
                return u, -1, "robots"
            LIMITER.acquire(u, min_interval=ROBOTS.crawl_delay(u))
            try:
                if self.impersonate and cffi_requests is not None:
                    st, body = self._impersonate_text(u, headers)
                else:
                    st, body = get_text(u, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
                if st in (403, 429):
                    LIMITER.penalize(u)
                else:
                    LIMITER.reward(u)
                return u, st, body
            except Exception as e:
                return u, 0, str(e)[:150]

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(one, u) for u in urls]
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
    try:
        return json.loads(body)
    except Exception:
        pass
    idx = [i for i in (body.find("["), body.find("{")) if i >= 0]
    if not idx:
        return None
    try:
        return json.JSONDecoder().raw_decode(body[min(idx):])[0]
    except Exception:
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
