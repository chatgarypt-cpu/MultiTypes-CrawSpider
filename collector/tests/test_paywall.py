"""13ft 付费墙回退：候选顺序 / 判重 / 预算 / 与采集主链路的对接。

全部离线：monkeypatch `paywall._get` 或 `paywall.recover`，不访问外网、不写真实 data/。
"""
import logging

import pytest

from crawler import paywall
from crawler.channels import base

LOG = logging.getLogger(__name__)
GOOD = ("<html><head><title>Story</title></head><body><article>"
        + ("article body text " * 200) + "</article></body></html>")
CHALLENGE = ("<html><head><title>Just a moment...</title></head>"
             "<body>cf-chl-bypass</body></html>")
SITE = {"domain": "example.test", "channel": "sitemap",
        "base": "https://example.test", "paywall_fallback": "13ft"}


@pytest.fixture(autouse=True)
def fresh_budget():
    paywall.reset_budget()
    yield
    paywall.reset_budget()


class FakeHTTP:
    """(URL 片段, UA 片段) → 假响应；没有匹配的路由一律当作连接失败。"""

    def __init__(self):
        self.calls = []
        self.routes = []

    def route(self, url_part, ua_part="", status=200, body="", final=""):
        self.routes.append((url_part, ua_part, status, body, final))
        return self

    def __call__(self, url, headers, timeout=(10, 25), allow_redirects=True, verify=True):
        ua = (headers or {}).get("User-Agent", "")
        self.calls.append((url, ua))
        matched = [r for r in self.routes if r[0] in url and r[1] in ua]
        if not matched:
            return 0, "", url
        # 最具体的路由优先：快照 URL 里也含有原站片段
        _, _, status, body, final = max(matched, key=lambda r: len(r[0]))
        return status, body, final or url


def test_mode_aliases_and_disabled_by_default():
    assert paywall.mode(SITE) == "13ft"
    assert paywall.mode({"paywall_fallback": " wayback "}) == "archive"
    assert not paywall.enabled({})
    assert not paywall.enabled({"paywall_fallback": "off"})


def test_disabled_site_returns_input_untouched(monkeypatch):
    http = FakeHTTP()
    monkeypatch.setattr(paywall, "_get", http)
    assert paywall.recover("https://example.test/a", {}, "<html>403</html>", 403) == \
        (403, "<html>403</html>", "")
    assert http.calls == []


def test_googlebot_path_is_tried_first(monkeypatch):
    http = FakeHTTP().route("example.test/story", "Googlebot", 200, GOOD)
    monkeypatch.setattr(paywall, "_get", http)
    status, body, source = paywall.recover("https://example.test/story", SITE, "", 403)
    assert (status, source) == (200, "googlebot")
    assert body == GOOD
    assert len(http.calls) == 1 and "Googlebot" in http.calls[0][1]


def test_challenge_page_is_rejected_then_archive_used(monkeypatch):
    http = (FakeHTTP()
            .route("example.test/story", "Googlebot", 200, CHALLENGE)
            .route("wayback/available", "", 200,
                   '{"archived_snapshots":{"closest":{"available":true,'
                   '"url":"https://web.archive.org/web/20240101000000/https://example.test/story"}}}')
            .route("web.archive.org/web/", "", 200, GOOD))
    monkeypatch.setattr(paywall, "_get", http)
    status, body, source = paywall.recover("https://example.test/story", SITE, "", 403)
    assert (status, source) == (200, "archive_org")
    assert any("/web/20240101000000id_/" in url for url, _ in http.calls)


def test_archive_today_when_wayback_empty(monkeypatch):
    http = (FakeHTTP()
            .route("example.test/story", "Googlebot", 403, "")
            .route("newest/", "", 200, GOOD, final="https://archive.ph/AbCdE1"))
    monkeypatch.setattr(paywall, "_get", http)
    status, _, source = paywall.recover("https://example.test/story", SITE, "", 403)
    assert (status, source) == (200, "archive_today")


def test_identical_body_is_not_counted_as_recovery(monkeypatch):
    http = FakeHTTP().route("example.test/story", "Googlebot", 200, GOOD)
    monkeypatch.setattr(paywall, "_get", http)
    status, body, source = paywall.recover("https://example.test/story", SITE, GOOD, 403)
    assert source == "" and body == GOOD and status == 403


def test_budget_blocks_extra_requests(monkeypatch):
    http = FakeHTTP().route("example.test/story", "Googlebot", 200, GOOD)
    monkeypatch.setattr(paywall, "_get", http)
    paywall.reset_budget(0)
    assert paywall.recover("https://example.test/story", SITE, "", 403)[2] == ""
    assert http.calls == []


def test_usable_rejects_thin_and_challenge_bodies():
    assert not paywall.usable("")
    assert not paywall.usable("<html>short</html>")
    assert not paywall.usable(CHALLENGE * 20)
    assert paywall.usable(GOOD)


def test_collect_urls_recovers_blocked_detail(tmp_env, monkeypatch):
    urls = ["https://example.test/story-1"]
    monkeypatch.setattr(base.paywall, "recover",
                        lambda url, site, body="", status=0: (200, GOOD, "googlebot"))
    monkeypatch.setattr(base, "extract_best",
                        lambda url, body, site, min_text=0: {
                            "url": url, "title": "Story", "text": "article body text " * 40})

    class Fetcher:
        def stream_requests(self, batch, workers=4):
            yield from ((req, 403, "<html><body>Forbidden</body></html>") for req in batch)

    result = base.collect_urls(SITE, urls, 0, LOG, Fetcher())
    assert result["saved"] == 1
    assert result["reasons"].get("paywall_googlebot") == 1


def test_collect_urls_keeps_403_when_fallback_disabled(tmp_env, monkeypatch):
    urls = ["https://example.test/story-1"]
    site = {k: v for k, v in SITE.items() if k != "paywall_fallback"}
    monkeypatch.setattr(base.paywall, "recover",
                        lambda url, site, body="", status=0: (status, body, ""))

    class Fetcher:
        def stream_requests(self, batch, workers=4):
            yield from ((req, 403, "<html><body>Forbidden</body></html>") for req in batch)

    result = base.collect_urls(site, urls, 0, LOG, Fetcher())
    assert result["saved"] == 0
    assert result["queue"] == {"error": 1}


def test_premium_marker_triggers_recovery(tmp_env, monkeypatch):
    urls = ["https://example.test/story-2"]
    site = dict(SITE, premium_marker='id="paywall"')
    blocked = ('<html><body><div id="paywall">Subscribe to read</div>'
               + ("teaser " * 100) + "</body></html>")
    monkeypatch.setattr(base.paywall, "recover",
                        lambda url, site, body="", status=0: (200, GOOD, "archive_org"))
    monkeypatch.setattr(base, "extract_best",
                        lambda url, body, site, min_text=0: {
                            "url": url, "title": "Story", "text": "article body text " * 40})

    class Fetcher:
        def stream_requests(self, batch, workers=4):
            yield from ((req, 200, blocked) for req in batch)

    result = base.collect_urls(site, urls, 0, LOG, Fetcher())
    assert result["saved"] == 1
    assert result["reasons"].get("paywall_archive_org") == 1


def test_same_truncated_article_with_new_token_is_not_recovery(monkeypatch):
    """Googlebot 拿到的还是同一份付费墙摘要（只换了 nonce）→ 继续往存档源找。"""
    teaser = "<html><body><article>" + ("teaser paragraph " * 60) + "</article><!--nonce-1--></body></html>"
    refreshed = teaser.replace("nonce-1", "nonce-2")
    http = (FakeHTTP()
            .route("example.test/story", "Googlebot", 200, refreshed)
            .route("wayback/available", "", 200, '{"archived_snapshots":{}}')
            .route("newest/", "", 200, GOOD, final="https://archive.ph/Zz9Yy8"))
    monkeypatch.setattr(paywall, "_get", http)
    status, body, source = paywall.recover("https://example.test/story", SITE, teaser, 200)
    assert (status, source) == (200, "archive_today")


def test_service_mode_posts_url_to_self_hosted_13ft(monkeypatch):
    """自建 13ft（docker compose，POST /article）作为另一种出口。"""
    calls = []

    class Resp:
        status_code = 200
        url = "http://127.0.0.1:9982/https://example.test/story"
        text = GOOD

        def close(self):
            pass

    def fake_post(url, data=None, headers=None, timeout=None, allow_redirects=True):
        calls.append((url, data))
        return Resp()

    monkeypatch.setattr(paywall.SESSION, "post", fake_post)
    site = {"paywall_fallback": "13ft-service", "paywall_service": "http://127.0.0.1:9982"}
    status, body, source = paywall.recover("https://example.test/story", site, "", 403)
    assert (status, source) == (200, "service")
    assert calls == [("http://127.0.0.1:9982/article", {"link": "https://example.test/story"})]


def test_service_mode_without_base_makes_no_request(monkeypatch):
    monkeypatch.delenv("PAYWALL_SERVICE_URL", raising=False)
    assert paywall.recover("https://example.test/story", {"paywall_fallback": "service"}, "", 403)[2] == ""


def test_preview_sample_uses_fallback(monkeypatch):
    """试采预览面板同样能拿到回退后的正文，而不是直接报 access_restricted。"""
    import web_preview

    site = dict(SITE, premium_marker='id="paywall"')
    monkeypatch.setattr(web_preview.paywall, "recover",
                        lambda url, site, body="", status=0: (200, GOOD, "googlebot"))
    monkeypatch.setattr(web_preview, "extract_best",
                        lambda url, body, site, min_text=0: {
                            "url": url, "title": "Story", "text": "article body text " * 40})
    blocked = ('<html><body><div id="paywall">Subscribe</div>'
               + ("teaser " * 100) + "</body></html>")
    sample, issue = web_preview._html_sample("https://example.test/a", 200, blocked, site, {})
    assert issue is None
    assert sample["title"] == "Story" and sample["text_length"] > 0
