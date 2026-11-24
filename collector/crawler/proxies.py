"""出口代理池：给采集换"虚拟 IP"（HTTP/HTTPS/SOCKS5），按域粘性 + 失败冷却 + 健康检查。

为什么需要：13ft 那类回退能绕付费墙，但绕不过"按出口 IP 拦"的站（tokelau.org.nz 的
CloudFront 403、部分 Cloudflare 1006）和按 IP 计数的限流。换出口比换 UA 有用。

用法（默认关闭，站点显式声明才生效）：

    - domain: www.tokelau.org.nz
      via: proxy
      proxy_pool: data/proxies.txt      # 一行一个；也可写 http://user:pass@host:port
      proxy_sticky: true                # 同一域名固定用同一个出口（默认 true）

也可以用内存列表（测试/临时）：

    from crawler.proxies import pool_for
    pool = pool_for(["http://127.0.0.1:8080", "socks5://127.0.0.1:1080"])

策略：
- 轮询 + 每域粘性；粘住的出口失败会立刻换下一个，并进入冷却；
- 连续失败 `max_failures`（默认 3）次 → 冷却 `cooldown`（默认 300s），期间不再选中；
- 全部冷却中时返回 None，调用方回退到直连（不会静默失败）；
- 代理字符串在日志里脱敏（`mask()`），密码不进 run.log。
"""
import os
import re
import threading
import time
from urllib.parse import urlsplit

from crawler.config import DATA

POOL_FILE = os.environ.get("CRAWL_PROXY_FILE") or os.path.join(DATA, "proxies.txt")
STATE_FILE = os.environ.get("CRAWL_PROXY_STATE") or os.path.join(DATA, "proxy_state.json")
SCHEMES = ("http://", "https://", "socks5://", "socks5h://", "socks4://")
MAX_FAILURES = int(os.environ.get("PROXY_MAX_FAILURES", "3"))
COOLDOWN = float(os.environ.get("PROXY_COOLDOWN", "300"))


def normalize(entry):
    """`host:port` → `http://host:port`；保留 scheme 与认证信息。"""
    entry = (entry or "").strip()
    if not entry or entry.startswith("#"):
        return ""
    if not entry.startswith(SCHEMES):
        entry = "http://" + entry
    try:
        p = urlsplit(entry)
    except ValueError:
        return ""
    if not p.hostname or not p.port:
        return ""
    return entry


def mask(proxy):
    """日志里隐藏认证信息：http://user:pass@h:1 → http://***@h:1"""
    if not proxy:
        return ""
    try:
        p = urlsplit(proxy)
    except ValueError:
        return "***"
    host = p.hostname or ""
    port = f":{p.port}" if p.port else ""
    return f"{p.scheme}://***@{host}{port}" if p.username else f"{p.scheme}://{host}{port}"


def load(path=POOL_FILE):
    """读代理清单：文件不存在返回空列表（不是错误，等于"没有池"）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    return [p for p in (normalize(l) for l in lines) if p]


class Pool:
    def __init__(self, entries=(), sticky=True, max_failures=MAX_FAILURES, cooldown=COOLDOWN):
        self.entries = list(dict.fromkeys(p for p in (normalize(e) for e in entries) if p))
        self.sticky = sticky
        self.max_failures = max_failures
        self.cooldown = cooldown
        self._failures, self._cooled_at = {}, {}
        self._sticky_map = {}
        self._rr = 0
        self._lock = threading.Lock()

    # ---------- 选取 ----------
    def available(self):
        now = time.time()
        return [p for p in self.entries if now - self._cooled_at.get(p, 0) >= self.cooldown]

    def pick(self, url=""):
        """给一个 URL 找出口：优先该域粘住的代理，否则轮询；全冷却时返回 None（直连）。"""
        if not self.entries:
            return None
        host = (urlsplit(url).netloc or "").lower()
        with self._lock:
            alive = self.available()
            if not alive:
                return None
            pinned = self._sticky_map.get(host)
            if self.sticky and pinned in alive:
                return pinned
            proxy = alive[self._rr % len(alive)]
            self._rr += 1
            if self.sticky and host:
                self._sticky_map[host] = proxy
            return proxy

    def report(self, proxy, ok):
        if not proxy:
            return
        with self._lock:
            if ok:
                self._failures.pop(proxy, None)
                return
            n = self._failures.get(proxy, 0) + 1
            self._failures[proxy] = n
            if n >= self.max_failures:
                self._cooled_at[proxy] = time.time()
                # 冷却时解绑所有粘着它的域名，下一轮自然换出口
                for host, p in list(self._sticky_map.items()):
                    if p == proxy:
                        self._sticky_map.pop(host, None)

    # ---------- 观测 ----------
    def stats(self):
        now = time.time()
        cooked = [p for p, ts in self._cooled_at.items() if now - ts < self.cooldown]
        return {"entries": len(self.entries), "available": len(self.available()),
                "cooling": len(cooked), "sticky_hosts": len(self._sticky_map)}

    def snapshot(self):
        return {"entries": [mask(p) for p in self.entries], **self.stats()}


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def pool_for(source=None, sticky=True):
    """按"文件名或代理列表"取池实例；同一文件/列表共享一个池（进程内缓存）。"""
    if source is None:
        key, entries = ("__file__:" + POOL_FILE), load(POOL_FILE)
    elif isinstance(source, str):
        key, entries = ("file:" + source), load(source)
    else:
        key, entries = ("list:" + "|".join(source or [])), list(source or [])
    with _CACHE_LOCK:
        pool = _CACHE.get(key)
        if pool is None:
            pool = Pool(entries, sticky=sticky)
            _CACHE[key] = pool
        return pool


def check(proxy, timeout=8, probe="https://api.ipify.org?format=json"):
    """测一个出口：返回 (ok, 出口 IP 或错误)。"""
    import requests
    try:
        r = requests.get(probe, proxies={"http": proxy, "https": proxy}, timeout=timeout)
        if r.status_code != 200:
            return False, f"http_{r.status_code}"
        text = r.text.strip()
        m = re.search(r'"ip"\s*:\s*"([0-9a-fA-F:.]+)"', text)
        return True, m.group(1) if m else text[:60]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:80]


def healthcheck(entries=None, timeout=8):
    """并发探测整池，返回 [{"proxy": 脱敏, "ok": bool, "ip": ...}]，不做任何写入。"""
    from concurrent.futures import ThreadPoolExecutor
    items = list(entries or load())
    if not items:
        return []

    def one(p):
        ok, info = check(p, timeout=timeout)
        return {"proxy": mask(p), "ok": ok, "ip": info if ok else "", "error": "" if ok else info}

    with ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
        return list(ex.map(one, items))
