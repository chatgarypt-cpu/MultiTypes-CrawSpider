"""Bypass Paywalls Clean 规则：匹配、字段映射、与回退链的接入。"""
import pytest

from crawler import bpc, paywall

NYT = "https://www.nytimes.com/2024/01/01/us/politics/congress.html"
NYT_RULE = bpc.load()["sites"].get("The New York Times") or {}


def test_rules_loaded_from_upstream():
    data = bpc.load()
    assert data.get("count", 0) >= 400
    assert "The New York Times" in data["sites"]


def test_match_by_domain_and_absent():
    name, rule = bpc.match(NYT)
    assert name == "The New York Times" and rule
    assert bpc.match("https://www.samoaobserver.ws/x") == (None, None)


def test_match_group_domains():
    grouped = [(k, r) for k, r in bpc.load()["sites"].items() if r.get("group")]
    if not grouped:
        pytest.skip("规则库里没有 group 规则")
    name, rule = grouped[0]
    dom = rule["group"][0]
    assert bpc.match(f"https://www.{dom}/a")[0] == name


def test_excluded_domains_are_skipped():
    for name, rule in bpc.load()["sites"].items():
        for dom in rule.get("excluded_domains") or []:
            assert bpc.match(f"https://{dom}/x") == (None, None)
            return
    pytest.skip("规则库里没有 excluded_domains")


def test_plan_maps_upstream_fields():
    plan = bpc.plan(NYT)
    assert plan["site"] == "The New York Times"
    assert "Googlebot" in plan["useragent"]
    assert plan["referer"] == "https://www.google.com/"
    assert plan["block"]                       # block_regex 被翻译成正则源码


def test_headers_include_referer_and_ua():
    h = bpc.headers(NYT)
    assert h["Referer"].startswith("https://")
    assert "User-Agent" in h
    assert bpc.headers("https://www.samoaobserver.ws/x") == {}


def test_candidates_put_bpc_first_when_rule_exists(monkeypatch):
    monkeypatch.setattr(paywall, "_bpc", lambda url: (200, "x" * 800, url))
    chain = paywall._candidates(NYT, {"paywall_fallback": "13ft"})
    assert chain[0][0] == "bpc"


def test_candidates_skip_bpc_without_rule():
    chain = paywall._candidates("https://www.samoaobserver.ws/x", {"paywall_fallback": "13ft"})
    assert all(name != "bpc" for name, _ in chain)


def test_recover_uses_bpc_source(monkeypatch):
    body = "<html><body><article>" + ("bpc body text " * 120) + "</article></body></html>"
    monkeypatch.setattr(paywall, "_bpc", lambda url: (200, body, url))
    status, out, source = paywall.recover(NYT, {"paywall_fallback": "13ft"}, "", 403)
    assert (status, source) == (200, "bpc")
    assert out == body
