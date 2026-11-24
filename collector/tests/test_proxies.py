"""代理池：解析/脱敏/粘性/冷却/健康检查。全部离线。"""
import pytest

from crawler import proxies


def test_normalize_accepts_common_forms():
    assert proxies.normalize("127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert proxies.normalize(" socks5://1.2.3.4:1080 ") == "socks5://1.2.3.4:1080"
    assert proxies.normalize("http://user:pass@h.example:3128") == "http://user:pass@h.example:3128"
    assert proxies.normalize("# comment") == ""
    assert proxies.normalize("") == ""
    assert proxies.normalize("host-without-port") == ""


def test_mask_hides_credentials():
    masked = proxies.mask("http://user:secret@1.2.3.4:8080")
    assert masked == "http://***@1.2.3.4:8080"
    assert "secret" not in masked
    assert proxies.mask("http://1.2.3.4:8080") == "http://1.2.3.4:8080"


def test_pick_is_sticky_per_host_and_rotates_across_hosts():
    pool = proxies.Pool(["http://a.test:1", "http://b.test:2"])
    first = pool.pick("https://x.test/1")
    assert pool.pick("https://x.test/2") == first          # 同域粘住
    assert pool.pick("https://y.test/1") != first           # 换域才轮换


def test_failures_cool_down_and_unbind_sticky():
    pool = proxies.Pool(["http://a.test:1", "http://b.test:2"], max_failures=2, cooldown=60)
    a = pool.pick("https://x.test/1")
    pool.report(a, ok=False)
    pool.report(a, ok=False)
    assert a not in pool.available()
    assert pool.pick("https://x.test/2") != a
    assert pool.stats()["cooling"] == 1


def test_success_resets_failure_counter():
    pool = proxies.Pool(["http://a.test:1"], max_failures=2, cooldown=60)
    p = pool.pick("https://x.test/")
    pool.report(p, ok=False)
    pool.report(p, ok=True)
    pool.report(p, ok=False)          # 计数已重置，不应冷却
    assert pool.available() == [p]


def test_all_cooling_returns_none_so_caller_falls_back_to_direct():
    pool = proxies.Pool(["http://a.test:1"], max_failures=1, cooldown=60)
    p = pool.pick("https://x.test/")
    pool.report(p, ok=False)
    assert pool.pick("https://x.test/") is None


def test_healthcheck_reports_ip_and_masks(monkeypatch):
    monkeypatch.setattr(proxies, "check",
                        lambda p, timeout=8, probe="x": (True, "9.9.9.9") if "a.test" in p
                        else (False, "ProxyError"))
    res = proxies.healthcheck(["http://user:pw@a.test:1", "http://b.test:2"])
    assert [r["ok"] for r in res] == [True, False]
    assert res[0]["ip"] == "9.9.9.9"
    assert "pw" not in res[0]["proxy"]


def test_load_skips_comments_and_blank_lines(tmp_path):
    f = tmp_path / "proxies.txt"
    f.write_text("# pool\n\n127.0.0.1:8080\nsocks5://127.0.0.1:1080\n", encoding="utf-8")
    assert proxies.load(str(f)) == ["http://127.0.0.1:8080", "socks5://127.0.0.1:1080"]


def test_pool_for_caches_by_source():
    a = proxies.pool_for(["http://a.test:1"])
    b = proxies.pool_for(["http://a.test:1"])
    c = proxies.pool_for(["http://b.test:2"])
    assert a is b and a is not c
