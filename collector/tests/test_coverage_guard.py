"""覆盖率口径 guard 与缺失清单测试（全离线：网络函数用桩替换）。

审查项 3 的核心回归：本地远大于上游（如 5,043 ÷ 24）不能再算"采全"，
必须 coverage=None + verdict="口径异常"，且不计入 verified/complete。
"""
import pytest

import verify_coverage as vc


# ---------------------------------------------------------------- guard 三个边界
def test_guard_expected_none_is_anomaly():
    assert vc.guard(100, None) == (None, "口径异常")


def test_guard_expected_zero_is_anomaly():
    assert vc.guard(100, 0) == (None, "口径异常")


def test_guard_expected_far_smaller_than_local_is_anomaly():
    # 审查里的真实案例：本地 5,043、上游 24 => 210x，旧实现算 21012% 且记 "complete"
    assert vc.guard(5043, 24) == (None, "口径异常")


def test_guard_boundary_is_local_over_1_5():
    # expected == local/1.5 即异常边界；稍微大于边界才算正常
    assert vc.guard(100, 66) == (None, "口径异常")
    cov, verdict = vc.guard(100, 68)
    assert cov == pytest.approx(1.471) and verdict == "已核对"


def test_guard_normal_cases():
    assert vc.guard(97, 100) == (0.97, "已核对")
    assert vc.guard(95, 100) == (0.95, "已核对")
    assert vc.guard(94, 100) == (0.94, "有缺口")
    assert vc.guard(0, 10) == (0.0, "有缺口")


# ---------------------------------------------------------------- 缺失清单
def test_missing_payload_keeps_full_small_list_and_samples_large_one():
    small = vc.missing_payload([f"https://x/{i}" for i in range(3)])
    assert small["count"] == 3 and small["truncated"] is False
    assert small["urls"] == small["sample"] and len(small["sample"]) == 3

    large = vc.missing_payload([f"https://x/{i}" for i in range(60)])
    assert large["count"] == 60 and large["truncated"] is True
    assert large["urls"] is None and len(large["sample"]) == vc.MISSING_SAMPLE


def test_missing_urls_uses_canonical_keys_and_dedupes():
    known = {vc.canonical_key("https://x.test/news/a/")}     # 尾斜杠/大小写归一到同一 key
    upstream = ["https://X.test/news/a", "https://x.test/news/b",
                "https://x.test/news/b/", "https://x.test/news/c"]
    assert vc.missing_urls(upstream, known) == ["https://x.test/news/b", "https://x.test/news/c"]


# ---------------------------------------------------------------- verify_site: sitemap
@pytest.fixture
def local_urls(tmp_path, monkeypatch):
    """把 DATA 指到临时目录，写入某站已采 URL。"""
    monkeypatch.setattr(vc, "DATA", str(tmp_path))
    d = tmp_path / "site.test"
    d.mkdir()

    def _write(lines, name="urls.txt"):
        (d / name).write_text("".join(f"{u}\n" for u in lines), encoding="utf-8")

    return _write


def test_sitemap_normal_case_reports_missing_urls(monkeypatch, local_urls):
    upstream = [f"https://site.test/2026/01/post-{i}" for i in range(10)]
    local_urls(upstream[:8])                                  # 采了 8 / 10
    monkeypatch.setattr(vc, "sitemap_urls", lambda site: upstream)
    site = {"domain": "site.test", "channel": "sitemap", "base": "https://site.test"}
    rec = vc.verify_site(site, {"posts": 8, "pages": 0, "media": 0})
    assert rec["verdict"] == "有缺口" and rec["coverage"] == 0.8
    assert rec["missing"]["count"] == 2
    assert rec["missing"]["urls"] == upstream[8:]
    assert rec["expected"] == 10


def test_sitemap_scope_anomaly_is_not_verified(monkeypatch, local_urls):
    upstream = [f"https://site.test/2026/01/post-{i}" for i in range(24)]
    monkeypatch.setattr(vc, "sitemap_urls", lambda site: upstream)
    site = {"domain": "site.test", "channel": "sitemap", "base": "https://site.test"}
    rec = vc.verify_site(site, {"posts": 5043, "pages": 0, "media": 0})
    assert rec["coverage"] is None and rec["verdict"] == "口径异常"
    assert "明显小于本地" in rec["verdict_detail"]
    assert "missing" not in rec                         # 口径不对时不虚构缺失清单
    payload = vc.summarize([rec])
    assert payload["verified"] == 0 and payload["complete"] == 0
    assert payload["anomalous"] == 1


def test_sitemap_without_locs_is_anomaly_not_verified(monkeypatch, tmp_path):
    """sitemap 通道取不到 loc => expected 缺失，按口径异常处理，不能算核对过。"""
    monkeypatch.setattr(vc, "DATA", str(tmp_path))
    monkeypatch.setattr(vc, "sitemap_urls", lambda site: [])
    site = {"domain": "site.test", "channel": "sitemap", "base": "https://site.test"}
    rec = vc.verify_site(site, {"posts": 5, "pages": 0, "media": 0})
    assert rec["coverage"] is None and rec["verdict"] == "口径异常"
    payload = vc.summarize([rec])
    assert payload["anomalous"] == 1 and payload["verified"] == 0 and payload["complete"] == 0


def test_rss_without_sitemap_is_unverifiable(monkeypatch, tmp_path):
    """rss/html 本就没有独立总量源：按 unverifiable 记，计入"已核对（unverifiable）"。"""
    monkeypatch.setattr(vc, "DATA", str(tmp_path))
    monkeypatch.setattr(vc, "sitemap_urls", lambda site: [])
    site = {"domain": "site.test", "channel": "rss", "base": "https://site.test", "feed": "/feed/"}
    rec = vc.verify_site(site, {"posts": 5, "pages": 0, "media": 0})
    assert rec["coverage"] is None and rec["verdict"] == "不可验证"
    assert vc.summarize([rec])["unverifiable"] == 1 and vc.summarize([rec])["verified"] == 0


# ---------------------------------------------------------------- verify_site: wp-api
def test_wp_normal_case_keeps_per_kind_and_missing_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "DATA", str(tmp_path))
    monkeypatch.setattr(vc, "wp_totals",
                        lambda site: {"posts": 100, "pages": 10, "media": 50})
    monkeypatch.setattr(vc, "wp_missing_sample",
                        lambda site, kinds, **kw: {"sample": ["https://site.test/2026/01/gap"],
                                                   "sample_count": 1, "scanned": 200,
                                                   "note": "stub"})
    site = {"domain": "site.test", "channel": "wp-api", "base": "https://site.test"}
    rec = vc.verify_site(site, {"posts": 97, "pages": 10, "media": 49})
    assert rec["coverage"] == pytest.approx(0.97) and rec["verdict"] == "已核对"
    assert rec["per_kind"]["pages"] == 1.0
    assert rec["missing"]["count"] == 4                  # posts 3 + media 1
    assert rec["missing"]["per_kind"] == {"posts": 3, "media": 1}


def test_wp_scope_anomaly_blocks_whole_site(monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "DATA", str(tmp_path))
    monkeypatch.setattr(vc, "wp_totals",
                        lambda site: {"posts": 24, "pages": 10, "media": 50})
    site = {"domain": "site.test", "channel": "wp-api", "base": "https://site.test"}
    rec = vc.verify_site(site, {"posts": 5043, "pages": 10, "media": 50})
    assert rec["coverage"] is None and rec["verdict"] == "口径异常"
    assert "posts" in rec["verdict_detail"]


def test_wp_missing_headers_is_anomaly(monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "DATA", str(tmp_path))
    monkeypatch.setattr(vc, "wp_totals", lambda site: {"posts": None, "pages": None, "media": None})
    site = {"domain": "site.test", "channel": "wp-api", "base": "https://site.test"}
    rec = vc.verify_site(site, {"posts": 10, "pages": 0, "media": 0})
    assert rec["coverage"] is None and rec["verdict"] == "口径异常"
    assert "X-WP-Total 缺失" in rec["verdict_detail"]


# ---------------------------------------------------------------- 汇总口径
def test_summarize_anomaly_excluded_from_verified_and_complete():
    report = [
        {"domain": "ok.test", "verdict": "已核对", "coverage": 0.99},
        {"domain": "gap.test", "verdict": "有缺口", "coverage": 0.5},
        {"domain": "bad.test", "verdict": "口径异常", "coverage": None},
        {"domain": "unver.test", "verdict": "不可验证", "coverage": None},
        {"domain": "err.test", "verdict": "核对失败", "coverage": None},
    ]
    payload = vc.summarize(report, disabled=[{"domain": "off.test"}])
    assert payload["verified"] == 2                      # 有数值 coverage 的两个，口径异常不计入
    assert payload["complete"] == 1
    assert payload["anomalous"] == 1
    assert payload["unverifiable"] == 1
    assert payload["failed"] == 1
    assert payload["disabled"] == 1
