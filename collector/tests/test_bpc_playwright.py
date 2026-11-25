"""Playwright 侧的 BPC 拦截：纯函数 + 装配逻辑（不启动浏览器）。"""
from crawler import browser_playwright as bp

NYT = "https://www.nytimes.com/2026/11/24/climate/iowa-army-ammunition-plant-explostion.html"
PATTERNS = bp.bpc_patterns(NYT)


class FakeRoute:
    def __init__(self, url):
        self.request = type("Req", (), {"url": url})()
        self.result = None

    def abort(self):
        self.result = "abort"

    def fallback(self):
        self.result = "fallback"


class FakePage:
    def __init__(self):
        self.handlers = []
        self.styles = []
        self.navigation = []
        self.results = []
        self.main_frame = object()

    def route(self, pattern, handler):
        self.handlers.append((pattern, handler))

    def add_style_tag(self, content=None, **kw):
        self.styles.append((self.navigation[-1], content))

    def on(self, *_):
        pass

    def goto(self, url, **_):
        self.navigation.append(url)
        for _, handler in self.handlers:
            probe = FakeRoute("https://www.nytimes.com/meter.js")
            handler(probe)
            self.results.append(probe.result)
        return type("Response", (), {"status": 200})()

    def wait_for_timeout(self, *_):
        pass

    def content(self):
        return "<html>article</html>"

    def close(self):
        pass


def test_patterns_exist_for_ruled_site():
    assert PATTERNS, "NYT 规则应带 block_regex"


def test_should_block_matches_rule_targets_not_article():
    assert bp.should_block("https://www.nytimes.com/meter.js", PATTERNS) is True
    assert bp.should_block("https://mwcm.nyt.com/dcs/script.js", PATTERNS) is True
    assert bp.should_block(NYT, PATTERNS) is False
    assert bp.should_block("https://static01.nyt.com/app.js", PATTERNS) is False


def test_should_block_tolerates_bad_regex():
    assert bp.should_block("https://x.test/a", ["([unclosed"]) is False
    assert bp.should_block("https://x.test/a", []) is False


def test_bpc_streams_results_and_reuses_one_route_without_leaking_rules(monkeypatch):
    import pytest
    from crawler import bpc

    pytest.importorskip("playwright.sync_api")
    page = FakePage()
    unruled = "https://other.test/article"
    monkeypatch.setattr(bp, "_context", lambda **_: type("Context", (), {"new_page": lambda _: page})())
    monkeypatch.setattr(bpc, "plan", lambda url: {"block": PATTERNS, "amp_unhide": True} if url == NYT else {})
    outcomes = bp.iter_fetch([NYT, unruled, NYT], bpc_rules=True)

    assert next(outcomes)[:2] == (NYT, 200)
    assert page.navigation == [NYT]       # first result precedes the other navigations
    assert len(list(outcomes)) == 2
    assert len(page.handlers) == 1
    assert page.results == ["abort", "fallback", "abort"]
    assert page.styles == [(NYT, bp.AMP_UNHIDE_CSS)] * 2
