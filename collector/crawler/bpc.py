"""Bypass Paywalls Clean 规则的采集端执行器。

规则来自上游 `sites.js`（MIT，magnolia1234，https://gitlab.com/magnolia1234/bypass-paywalls-clean-filters），
原样放在 `crawler/bpc_rules/`，由 `export.mjs` 序列化成 `rules.json`；这里只做匹配与执行，
**不改写规则**，字段语义照搬上游：

| 字段 | 上游语义 | 采集端执行 |
|---|---|---|
| `domain` / `group` | 生效域名（group 是同一媒体集团的多站） | 后缀匹配，取最长 |
| `excluded_domains` | 例外域名 | 命中则跳过 |
| `useragent: googlebot/bingbot` | 伪装成搜索引擎爬虫 | 换 UA |
| `allow_cookies` | 允许携带 cookie（否则上游会清） | 我们用无 cookie 会话，等效"清 cookie" |
| `remove_cookies` | 指定要删的计量 cookie | 无 cookie 会话覆盖；同时记录供浏览器路径用 |
| `block_regex` | 拦截付费墙脚本（meter/paywall js） | requests 无法拦截；返回给 playwright 的 route 用 |
| `referer` | 伪造来源 | 设 Referer |
| `random_ip` | 伪造 X-Forwarded-For | 随机 IP 头 |
| `amp_unhide` / `ld_json` | AMP 解锁 / JSON-LD 取全文 | 标记，交给 htmlpage 处理 |
"""
import json
import os
import random
import re
from functools import lru_cache
from urllib.parse import urlsplit

RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bpc_rules", "rules.json")

GOOGLEBOT_UA = ("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.119 Mobile Safari/537.36 "
                "(compatible; Googlebot/2.1; +http://www.google.com/bot.html)")
BINGBOT_UA = ("Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)")
UA_ALIASES = {"googlebot": GOOGLEBOT_UA, "bingbot": BINGBOT_UA}
DEFAULT_REFERER = "https://www.google.com/"


@lru_cache(maxsize=1)
def load():
    """读 rules.json；文件缺失或坏掉都返回空表（调用方退回原有回退链）。"""
    try:
        with open(RULES_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"sites": {}, "count": 0}


@lru_cache(maxsize=1)
def _index():
    """host 后缀 → (站点名, 规则)；group 里的每个域名都建索引。"""
    idx = {}
    for name, rule in (load().get("sites") or {}).items():
        if not isinstance(rule, dict):
            continue
        domains = []
        dom = (rule.get("domain") or "").strip().lower()
        if dom and not dom.startswith("###"):
            domains.append(dom)
        for g in rule.get("group") or []:
            if isinstance(g, str) and g.strip():
                domains.append(g.strip().lower())
        for d in domains:
            d = d.strip(".")
            if d and (d not in idx or len(d) > len(idx[d][1])):
                idx[d] = (name, d, rule)
    return idx


def _host(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _excluded(host, rule):
    for dom in rule.get("excluded_domains") or []:
        if isinstance(dom, str) and host and (host == dom.lower() or host.endswith("." + dom.lower())):
            return True
    return False


def match(url):
    """返回 (站点名, 规则) 或 (None, None)。"""
    host = _host(url)
    if not host:
        return None, None
    idx = _index()
    best = None
    for dom, (name, d, rule) in idx.items():
        if host == d or host.endswith("." + d):
            if best is None or len(d) > len(best[1]):
                best = (name, d, rule)
    if not best:
        return None, None
    name, _, rule = best
    if _excluded(host, rule):
        return None, None
    return name, rule


def _regex_list(rule, key):
    out = []
    for item in [rule.get(key)] if not isinstance(rule.get(key), list) else rule.get(key):
        if isinstance(item, dict) and item.get("__regex"):
            out.append(item["__regex"])
        elif isinstance(item, str) and item:
            out.append(item)
    return out


def plan(url):
    """把一条规则翻译成采集端可执行的动作；未命中返回 {}。"""
    name, rule = match(url)
    if not rule:
        return {}
    ua = (rule.get("useragent") or "").strip().lower()
    referer = rule.get("referer") or DEFAULT_REFERER
    if isinstance(referer, dict):
        referer = DEFAULT_REFERER
    return {
        "site": name,
        "useragent": UA_ALIASES.get(ua, UA_ALIASES["googlebot"] if ua else ""),
        "referer": referer,
        "random_ip": bool(rule.get("random_ip")),
        "drop_cookies": [c for c in (rule.get("remove_cookies") or []) if isinstance(c, str)],
        "block": _regex_list(rule, "block_regex") + _regex_list(rule, "block_regex_general"),
        "amp_unhide": bool(rule.get("amp_unhide")),
        "ld_json": rule.get("ld_json") if isinstance(rule.get("ld_json"), str) else "",
        "allow_cookies": bool(rule.get("allow_cookies")),
    }


def headers(url):
    """BPC 规定的请求头（未命中返回 {}）。"""
    p = plan(url)
    if not p:
        return {}
    h = {"Accept": "*/*"}
    if p["useragent"]:
        h["User-Agent"] = p["useragent"]
    if p["referer"]:
        h["Referer"] = p["referer"]
    if p["random_ip"]:
        h["X-Forwarded-For"] = "%d.%d.%d.%d" % (random.randint(11, 223), random.randint(0, 255),
                                                random.randint(0, 255), random.randint(1, 254))
        h["X-Real-IP"] = h["X-Forwarded-For"]
    return h
