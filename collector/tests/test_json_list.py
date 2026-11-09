"""json-list 通道性能/正确性修复的离线测试（2026-11-09）。"""
import json
import os

import pytest

from crawler.channels import json_list


class Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(json_list.time, "sleep", lambda *_a, **_k: None)
    for name in ("acquire", "penalize", "reward"):
        monkeypatch.setattr(json_list.LIMITER, name, lambda *_a, **_k: None)


def test_get_json_retries_429_then_ok(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return Resp(429) if len(calls) == 1 else Resp(200, {"stories": [{"id": 7}]})

    monkeypatch.setattr(json_list.SESSION, "get", fake_get)
    data, err = json_list._get_json("https://x.test/p/1.json")
    assert err is None and data["stories"][0]["id"] == 7 and len(calls) == 2


def test_get_json_gives_up_with_error(monkeypatch):
    monkeypatch.setattr(json_list.SESSION, "get", lambda *a, **k: Resp(503))
    data, err = json_list._get_json("https://x.test/p/1.json")
    assert data is None and err == "http_503"


def test_fetch_page_parallel_categories(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        cat = url.split("category=")[1].split("&")[0]
        return Resp(200, {"stories": [{"id": cat}]})

    monkeypatch.setattr(json_list.SESSION, "get", fake_get)
    pat = "/stories/page/{page}.json?category={category}&api=true"
    data, errors = json_list.fetch_page(pat, "https://x.test", 1, ["a", "b"], workers=2)
    assert not errors
    assert set(data) == {"a", "b"} and data["a"][0]["id"] == "a"


def test_incremental_stops_when_pages_all_known(tmp_env, monkeypatch):
    site = {"domain": "inc.test", "base": "https://inc.test", "channel": "json-list",
            "api_list": "/p/{page}.json?category={category}", "api_categories": ["a"],
            "api_url": "{base}/item/{id}", "scan_mode": "incremental",
            "known_pages_stop": 3, "pages": 1000}
    monkeypatch.setattr(json_list, "DATA", str(tmp_env))
    monkeypatch.setattr(json_list, "load_known",
                        lambda d, k="posts": {"https://inc.test/item/1"})
    monkeypatch.setattr(json_list, "fetch_page",
                        lambda *a, **k: ({"a": [{"id": 1}]}, []))
    res = json_list.collect_json_list(site, 0)
    assert res["saved"] == 0
    cur = json.load(open(os.path.join(str(tmp_env), "inc.test", "jsonlist.cursor.json")))
    assert cur["page"] == 1                  # 增量模式下次仍从第一页找新内容


def test_full_mode_resumes_from_cursor(tmp_env, monkeypatch):
    site = {"domain": "full.test", "base": "https://full.test", "channel": "json-list",
            "api_list": "/p/{page}.json?category={category}", "api_categories": ["a"],
            "api_url": "{base}/item/{id}", "max_pages_per_run": 2, "pages": 1000}
    d = os.path.join(str(tmp_env), "full.test")
    os.makedirs(d, exist_ok=True)
    json.dump({"page": 5}, open(os.path.join(d, "jsonlist.cursor.json"), "w"))
    monkeypatch.setattr(json_list, "DATA", str(tmp_env))
    monkeypatch.setattr(json_list, "load_known", lambda d, k="posts": set())
    monkeypatch.setattr(json_list, "fetch_page", lambda *a, **k: ({"a": []}, []))
    json_list.collect_json_list(site, 0)
    # 空页 -> 翻到底；游标保持（不写）或写到下一页，不应回到 1
    cur = json.load(open(os.path.join(d, "jsonlist.cursor.json")))
    assert cur["page"] >= 5
