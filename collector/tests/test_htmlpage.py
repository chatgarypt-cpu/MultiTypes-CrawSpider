"""HTML 页面流水线（crawler.htmlpage）与发现层修复的离线测试。

覆盖：剪枝择优、列表页闸门、XPath schema、可选 LLM、html_urls 的 urljoin/资源过滤/item_selector。
"""
import sys

import pytest

from crawler import htmlpage
from crawler.extract import extract_best
from crawler.discover import html_urls


ARTICLE = """<html><head>
<meta property="og:title" content="Clean Title">
<meta property="article:published_time" content="2026-10-08T00:00:00Z">
</head><body>
<header><nav><a href="/">Home</a><a href="/news">News</a><a href="/about">About</a></nav></header>
<article><h1>Clean Title</h1>
<p>The Office of the Secretary to Government wishes to advise all public service employees that
phishing emails are circulating and reports indicate that some users have already been affected.</p>
<p>People should not click links in unexpected messages, should verify the sender, and should report
suspicious mail to the service desk so that the incident can be investigated.</p>
</article>
<footer><a href="/privacy">Privacy</a></footer>
</body></html>"""

LISTING = """<html><body>
<nav><a href="/">Home</a><a href="/news">News</a><a href="/contact">Contact</a></nav>
<ul>
<li><a href="/media-releases/a">Niue and Papua New Guinea sign framework agreement</a></li>
<li><a href="/media-releases/b">Joint statement between the leaders of the realm</a></li>
<li><a href="/media-releases/c">Prime Minister co-hosted realm leaders meeting</a></li>
<li><a href="/media-releases/d">Security notice on phishing emails</a></li>
</ul></body></html>"""


def test_markdown_keeps_article_drops_nav():
    md = htmlpage.markdown(ARTICLE, "https://example.test/a")
    assert "phishing emails are circulating" in md
    assert "Privacy" not in md


def test_listing_detection():
    md = htmlpage.markdown(LISTING, "https://example.test/list")
    assert htmlpage.link_ratio(LISTING) > 0.5
    assert htmlpage.looks_like_listing(LISTING, md) is True
    assert htmlpage.looks_like_listing(ARTICLE, htmlpage.markdown(ARTICLE)) is False


def test_xpath_schema_overrides_fields():
    body = '<html><body><h1>H1 Title</h1><time datetime="2026-01-02T03:04:05Z"></time>' \
           '<div class="body">x</div></body></html>'
    row = htmlpage.apply_schema({}, body, {"schema": {
        "title": "//h1/text()", "created_at": "//time/@datetime"}})
    assert row == {"title": "H1 Title", "created_at": "2026-01-02T03:04:05Z"}


def test_llm_disabled_without_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert htmlpage.llm_config() is None
    assert htmlpage.llm_extract("some markdown", ["title"]) == {}


def test_llm_fills_only_empty_fields(monkeypatch):
    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": '{"title": "LLM Title", "created_at": "2026-01-01"}'}}]}

    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setattr(htmlpage, "_post_json", lambda *a, **k: Resp())
    row = htmlpage.llm_enrich({"title": "", "created_at": "2020-01-01"},
                              "md", {"llm_fields": ["title", "created_at"]})
    assert row["title"] == "LLM Title"
    assert row["created_at"] == "2020-01-01"          # 非空字段不被覆盖


def test_llm_bad_json_is_silent(monkeypatch):
    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "not json"}}]}

    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setattr(htmlpage, "_post_json", lambda *a, **k: Resp())
    assert htmlpage.llm_extract("md", ["title"]) == {}


def test_extract_best_prefers_pruned_text():
    """粗抽整页（含导航）比正文长，旧逻辑按长度择优会永远选到导航文本。"""
    row = extract_best("https://example.test/a", ARTICLE, {"channel": "html", "domain": "example.test"}, min_text=0)
    assert row is not None
    assert row["_extractor"] == "trafilatura"
    assert "Clean Title" in row["text"]
    assert "Privacy" not in row["text"]


def test_extract_best_gate_rejects_listing():
    row = extract_best("https://example.test/list", LISTING,
                       {"channel": "html", "domain": "example.test"}, min_text=0)
    assert row is None


def test_gate_can_be_disabled_per_site():
    row = extract_best("https://example.test/list", LISTING,
                       {"channel": "html", "domain": "example.test", "noise_gate": False}, min_text=0)
    assert row is not None


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages

    def get_many(self, urls):
        return [(u, 200, self.pages.get(u, "")) for u in urls]

    def stream_many(self, urls, workers=4):
        return iter(self.get_many(urls))


def test_html_urls_resolves_relative_against_listing_page():
    site = {"domain": "example.test", "base": "https://example.test", "channel": "html",
            "list_urls": ["/media-releases"], "link_pattern": r"/media-releases/(?!page:)[^/]+$"}
    page = """<a href="a-story">a</a><a href="/media-releases/page:2">2</a>
              <a href="/media-releases/other">o</a><a href="/css/x.css">css</a>"""
    urls = html_urls(site, FakeFetcher({"https://example.test/media-releases": page}), 100)
    assert urls == ["https://example.test/media-releases/other"]


def test_html_urls_item_selector_and_assets():
    site = {"domain": "example.test", "base": "https://example.test", "channel": "html",
            "list_url": "/announcements", "item_selector": "//a[contains(@href,'/announcements/')]/@href"}
    page = """<a href="/announcements/1-one">one</a><a href="/announcements/x.png">png</a>
              <a href="/other">other</a>"""
    urls = html_urls(site, FakeFetcher({"https://example.test/announcements": page}), 100)
    assert urls == ["https://example.test/announcements/1-one"]
