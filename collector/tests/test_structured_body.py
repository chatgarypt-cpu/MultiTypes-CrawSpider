"""结构化数据正文：__NEXT_DATA__ / JSON-LD articleBody / __NUXT__ / __INITIAL_STATE__。"""
import json

import pytest

from crawler import extract, htmlpage

PARA = "The council approved the new budget on Tuesday. " * 12


def next_data(content):
    return ('<html><body><div id="root"></div>'
            f'<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": {"pageProps": {"article": {"content": content}}}})
            + '</script></body></html>')


def test_next_data_body_is_extracted():
    body = next_data(f"<p>{PARA}</p><p>{PARA}</p>")
    text = htmlpage.structured_body(body)
    assert len(text) > 1000 and "council approved" in text


def test_jsonld_article_body_is_extracted():
    payload = {"@type": "Article", "isAccessibleForFree": False, "articleBody": PARA * 2}
    body = ('<html><head><script type="application/ld+json">' + json.dumps(payload)
            + '</script></head><body><div class="paywall">Subscribe</div></body></html>')
    assert len(htmlpage.structured_body(body)) > 1000


def test_nuxt_and_initial_state_are_extracted():
    nuxt = ('<html><body><script>window.__NUXT__={"data":[{"story":{"body":"'
            + PARA * 2 + '"}}]};</script></body></html>')
    initial = ('<html><body><script>window.__INITIAL_STATE__={"read":{"content":"'
               + PARA * 2 + '"}};</script></body></html>')
    assert len(htmlpage.structured_body(nuxt)) > 1000
    assert len(htmlpage.structured_body(initial)) > 1000


def test_plain_page_has_no_structured_body():
    assert htmlpage.structured_body("<html><body><nav>menu</nav><p>short</p></body></html>") == ""


def test_short_strings_and_base64_are_rejected():
    blob = json.dumps({"props": {"token": "A" * 900, "content": "short text"}})
    body = f'<html><body><script id="__NEXT_DATA__" type="application/json">{blob}</script></body></html>'
    assert htmlpage.structured_body(body) == ""


def test_extract_best_prefers_structured_when_dom_is_paywalled():
    body = next_data(f"<p>{PARA}</p><p>{PARA}</p>")
    row = extract.extract_best("https://x.test/a", body, {"domain": "x.test", "base": "https://x.test"}, min_text=0)
    assert row and row["_extractor"] == "structured_json"
    assert len(row["text"]) > 1000
