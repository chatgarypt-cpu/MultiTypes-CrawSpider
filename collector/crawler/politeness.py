"""Per-domain politeness: adaptive token bucket + robots.txt gate.

Borrowed from Scrapy's AutoThrottle: a domain that answers 403/429 is slowed down,
and the delay decays back once requests succeed again. Everything is per-process,
which is enough for our single-machine, multi-process setup.

robots.txt:
- 只认 `User-agent: *` 组：GPTBot/Amazonbot 等爬虫的 `Disallow: /` 不能当全站禁止。
- 支持 `Crawl-delay`：作为该域每请求最小间隔（web 层调 acquire(..., min_interval=)）。
- 解析结果持久化到 data/robots.json，默认 TTL 24h；`ROBOTS_CACHE=0` 可关闭（测试用）。
"""
import json
import os
import threading
import time
from urllib.parse import urlsplit

import requests

from crawler.config import UA, DATA

DEFAULT_RATE = float(os.environ.get("CRAWL_RATE", "2.0"))   # 每域起始速率，可用 CRAWL_RATE 覆盖
MIN_RATE = 0.25
MAX_RATE = float(os.environ.get("CRAWL_MAX_RATE", "8.0"))   # 成功累积后回速的上限

ROBOTS_TTL_DEFAULT = 24 * 3600                                # 24h
ROBOTS_CACHE_PATH = os.environ.get("ROBOTS_CACHE_PATH") or os.path.join(DATA, "robots.json")
_FALSE_VALUES = ("0", "false", "off", "no")


def _env_enabled(name):
    return str(os.environ.get(name, "1")).strip().lower() not in _FALSE_VALUES


class DomainLimiter:
    def __init__(self, rate=DEFAULT_RATE):
        self.rate = {}                 # domain -> req/s
        self.next_at = {}              # domain -> monotonic timestamp
        self.streak = {}               # domain -> consecutive successes
        self.lock = threading.Lock()
        self._default = rate

    def _rate(self, domain):
        return self.rate.get(domain, self._default)

    def acquire(self, url, timeout=30, min_interval=None):
        """Wait for this domain's next slot.

        `min_interval` (seconds) comes from robots.txt Crawl-delay and acts as a
        per-domain lower bound between requests: effective interval = max(1/rate, crawl-delay).
        """
        domain = urlsplit(url).netloc
        with self.lock:
            now = time.monotonic()
            interval = 1.0 / self._rate(domain)
            if min_interval:
                try:
                    interval = max(interval, float(min_interval))
                except (TypeError, ValueError):
                    pass
            wait = self.next_at.get(domain, 0) - now
            self.next_at[domain] = max(now, self.next_at.get(domain, now)) + interval
        if wait > 0:
            time.sleep(min(wait, timeout))

    def penalize(self, url):
        domain = urlsplit(url).netloc
        with self.lock:
            self.rate[domain] = max(MIN_RATE, self._rate(domain) / 2)
            self.streak[domain] = 0

    def reward(self, url):
        domain = urlsplit(url).netloc
        with self.lock:
            streak = self.streak.get(domain, 0) + 1
            self.streak[domain] = streak
            if streak % 20 == 0:                      # recover slowly
                self.rate[domain] = min(MAX_RATE, self.rate.get(domain, self._default) * 1.5)


def _prefix(pattern):
    """Prefix that a robots path pattern can match before its first wildcard."""
    base = str(pattern).split("*", 1)[0]
    return base[:-1] if base.endswith("$") else base


def parse_robots(text):
    """Parse robots.txt; return (rules, crawl_delay) for the `*` group only.

    rules: list of ``(kind, path)`` with kind in {"allow", "disallow"}.
    多个 `*` 组按 robots 规范合并；Crawl-delay 取最大值（最保守）。
    """
    groups = {}          # agent -> {"rules": [...], "crawl_delay": None}
    cur = None           # user-agent lines of the group being built
    in_rules = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if in_rules:                     # 上一条规则组结束，开启新组
                cur = []
                in_rules = False
            if cur is None:
                cur = []
            cur.append(value.lower())
            continue
        if key not in ("disallow", "allow", "crawl-delay"):
            continue
        if cur is None:                      # 规则出现在任何 User-agent 之前：无效
            continue
        in_rules = True
        for agent in cur:
            group = groups.setdefault(agent, {"rules": [], "crawl_delay": None})
            if key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if delay >= 0:
                    old = group["crawl_delay"]
                    group["crawl_delay"] = delay if old is None else max(old, delay)
            else:
                group["rules"].append((key, value))
    group = groups.get("*", {"rules": [], "crawl_delay": None})
    return group["rules"], group["crawl_delay"]


def _allowed_path(path, rules):
    """Longest-match robots rule for one path; Allow wins ties (robots spec)."""
    allows = [p for kind, p in rules if kind == "allow" and p]
    disallows = [p for kind, p in rules if kind == "disallow" and p]
    best_allow = max((a for a in allows if path.startswith(_prefix(a))), key=lambda a: len(_prefix(a)), default="")
    best_disallow = max((d for d in disallows if path.startswith(_prefix(d))), key=lambda d: len(_prefix(d)), default="")
    if best_allow and len(_prefix(best_allow)) >= len(_prefix(best_disallow)):
        return True
    return not best_disallow


class RobotsCache:
    """robots.txt cache: allow on network error, obey the `*` group, honour Crawl-delay.

    `data/robots.json` persists {root: {ts, status, rules, crawl_delay}} with a TTL so
    process restarts don't refetch every robots.txt. Disable with ``ROBOTS_CACHE=0``.
    """

    def __init__(self, cache_path=None, ttl=None, persist=None, session=None, timeout=15):
        self._mem = {}
        self.cache = self._mem                    # 兼容旧属性名
        self.lock = threading.RLock()
        self.session = session
        self.timeout = timeout
        self.ttl = float(os.environ.get("ROBOTS_TTL", str(ROBOTS_TTL_DEFAULT))) if ttl is None else float(ttl)
        self.persist = _env_enabled("ROBOTS_CACHE") if persist is None else bool(persist)
        self.cache_path = cache_path or ROBOTS_CACHE_PATH
        self._disk = self._load_disk() if self.persist else {}

    # ---------- disk persistence ----------
    def _load_disk(self):
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries") if isinstance(data, dict) else None
            return entries if isinstance(entries, dict) else {}
        except Exception:
            return {}

    def _save_disk(self):
        if not self.persist:
            return
        try:
            folder = os.path.dirname(self.cache_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"_version": 1, "entries": self._disk}, f, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except Exception:
            pass

    # ---------- fetching / parsing ----------
    def _fetch_text(self, url):
        headers = {"User-Agent": UA}
        if self.session is not None:
            r = self.session.get(url, headers=headers, timeout=self.timeout)
        else:
            r = requests.get(url, headers=headers, timeout=self.timeout)
        return getattr(r, "status_code", 0), getattr(r, "text", "") or ""

    def _entry(self, root):
        now = time.time()
        with self.lock:
            entry = self._mem.get(root) or self._disk.get(root)
            if entry and now - float(entry.get("ts") or 0) < self.ttl:
                self._mem[root] = entry
                return entry
            try:
                status, text = self._fetch_text(root + "/robots.txt")
            except Exception:
                # 网络失败：本次放行，只放内存，不持久化（下次重试）
                entry = {"ts": now, "status": 0, "rules": [], "crawl_delay": None}
                self._mem[root] = entry
                return entry
            if int(status or 0) == 200:
                rules, delay = parse_robots(text)
                entry = {"ts": now, "status": 200, "rules": [list(r) for r in rules], "crawl_delay": delay}
            else:
                entry = {"ts": now, "status": int(status or 0), "rules": [], "crawl_delay": None}
            self._mem[root] = entry
            self._disk[root] = entry
            self._save_disk()
            return entry

    def _rules(self, url):
        p = urlsplit(url)
        root = f"{p.scheme}://{p.netloc}"
        entry = self._entry(root)
        return entry.get("rules") or [], entry.get("crawl_delay")

    # ---------- public API ----------
    def allowed(self, url):
        rules, _delay = self._rules(url)
        if not rules:
            return True
        return _allowed_path(urlsplit(url).path or "/", rules)

    def crawl_delay(self, url):
        """robots.txt Crawl-delay for this URL's domain, or None."""
        _rules, delay = self._rules(url)
        return delay

    def clear(self):
        with self.lock:
            self._mem.clear()
            self._disk.clear()
