"""status.py 载荷契约测试（前端"不完整"计数依赖 kinds.*.done）。"""
import json
import sys

import pytest


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


@pytest.fixture
def fake_status(tmp_path, monkeypatch):
    import status

    data = tmp_path / "data"
    data.mkdir()
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "sites:\n"  # 占位，真正内容用 yaml 列表
        "", encoding="utf-8")
    sources.write_text(
        "- domain: alpha.test\n  channel: sitemap\n  base: https://alpha.test\n"
        "- domain: beta.test\n  channel: wp-api\n  base: https://beta.test\n",
        encoding="utf-8")
    monkeypatch.setattr(status, "ROOT", str(tmp_path))
    monkeypatch.setattr(status, "DATA", str(data))

    # alpha: 非 WP 通道，游标 done=true, page=1/pages=1
    _write(data / "alpha.test" / "cursor.json",
           {"next_page": 1, "total_pages": 1, "saved": 3, "done": True,
            "status": "complete", "updated": "2026-11-07T00:00:00"})
    (data / "alpha.test" / "urls.txt").write_text("https://alpha.test/a\n", encoding="utf-8")
    # beta: WP，翻到一半
    _write(data / "beta.test" / "cursor.json",
           {"next_page": 3, "total_pages": 10, "saved": 100, "done": False,
            "status": "interrupted", "updated": "2026-11-07T00:00:00"})
    (data / "beta.test" / "urls.txt").write_text("https://beta.test/1\n", encoding="utf-8")
    # 运行目录 / 测试站不应出现在站点列表里
    for junk in ("jobs", "recon", "blog.python.org"):
        (data / junk).mkdir(exist_ok=True)
        (data / junk / "whatever.json").write_text("{}", encoding="utf-8")
    return status


def test_kinds_posts_carries_done_flag(fake_status):
    st = fake_status.build_status()
    alpha = next(r for r in st["sites"] if r["domain"] == "alpha.test")
    assert alpha["base_url"] == "https://alpha.test"
    assert alpha["kinds"]["posts"]["done"] is True
    assert alpha["kinds"]["posts"]["status"] == "complete"


def test_completed_non_wp_site_not_flagged_partial(fake_status):
    st = fake_status.build_status()
    alpha = next(r for r in st["sites"] if r["domain"] == "alpha.test")
    kinds = alpha["kinds"]
    # 前端规则：任一类 pages 已知且 !done 且 page<=pages => 不完整
    assert not any(
        k.get("pages") and not k.get("done") and (k.get("page") or 0) <= (k["pages"] or 0)
        for k in kinds.values()
    )


def test_half_finished_wp_site_flagged_partial(fake_status):
    st = fake_status.build_status()
    beta = next(r for r in st["sites"] if r["domain"] == "beta.test")
    kinds = beta["kinds"]
    assert any(
        k.get("pages") and not k.get("done") and (k.get("page") or 0) <= (k["pages"] or 0)
        for k in kinds.values()
    )


def test_sites_list_excludes_runtime_dirs_and_test_site(fake_status):
    st = fake_status.build_status()
    doms = {r["domain"] for r in st["sites"]}
    assert doms == {"alpha.test", "beta.test"}


# ---------- 覆盖率口径 / 缺入口 / disabled 三层字段（审查项 3） ----------
@pytest.fixture
def layered_status(tmp_path, monkeypatch):
    """任务结束(done)、覆盖率已核对(verified)、缺入口或不可访问(needs_config/disabled) 分开报。

    进程探测（pgrep/ps）不是本用例范围：沙箱常禁 ps，这里直接置空，保持离线。
    """
    import status

    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "sources.yaml").write_text(
        "- domain: done.test\n  channel: wp-api\n  base: https://done.test\n"
        "- domain: partial.test\n  channel: wp-api\n  base: https://partial.test\n"
        "- domain: unver.test\n  channel: rss\n  base: https://unver.test\n  feed: /feed/\n"
        "- domain: anomaly.test\n  channel: sitemap\n  base: https://anomaly.test\n  sitemap: /sitemap.xml\n"
        "- domain: config.test\n  channel: html\n  base: https://config.test\n"
        "- domain: gone.test\n  channel: html\n  base: https://gone.test\n"
        "  disabled: true\n  disabled_reason: \"站点已下线\"\n",
        encoding="utf-8")
    monkeypatch.setattr(status, "ROOT", str(tmp_path))
    monkeypatch.setattr(status, "DATA", str(data))
    monkeypatch.setattr(status, "running", lambda: False)
    monkeypatch.setattr(status, "queue_running", lambda: False)
    monkeypatch.setattr(status, "tasks_running", lambda: 0)
    monkeypatch.setattr(status, "running_domains", lambda: [])
    monkeypatch.setattr(status, "blocked_domains", lambda: [])

    # 已结束的站：cursor done
    _write(data / "done.test" / "cursor.json",
           {"next_page": 2, "total_pages": 2, "done": True, "status": "complete",
            "updated": "2026-11-09T00:00:00"})
    (data / "done.test" / "urls.txt").write_text("https://done.test/a\n", encoding="utf-8")
    # 覆盖率快照（只读来源）：覆盖三种核对结论 + 一条旧快照（无 verdict 但 coverage 异常）
    _write(data / "coverage-20261109.json", {"report": [
        {"domain": "done.test", "channel": "wp-api", "coverage": 0.97, "verdict": "已核对"},
        {"domain": "partial.test", "channel": "wp-api", "coverage": 0.6, "verdict": "有缺口"},
        {"domain": "unver.test", "channel": "rss", "coverage": None,
         "source": "unverifiable", "verdict": "不可验证"},
        {"domain": "anomaly.test", "channel": "sitemap", "coverage": 2.1},
    ]})
    return status


def _row(status, domain):
    st = status.build_status()
    return next(r for r in st["sites"] if r["domain"] == domain)


def test_per_site_rows_carry_three_layer_fields(layered_status):
    for dom in ("done.test", "partial.test", "unver.test", "anomaly.test",
                "config.test", "gone.test"):
        row = _row(layered_status, dom)
        for key in ("done", "verified", "coverage", "needs_config", "disabled"):
            assert key in row, f"{dom} 缺字段 {key}"


def test_verified_site_exposes_coverage(layered_status):
    row = _row(layered_status, "done.test")
    assert row["done"] is True and row["verified"] is True
    assert row["coverage"] == 0.97
    assert row["coverage_verdict"] == "已核对"


def test_low_coverage_is_not_verified(layered_status):
    row = _row(layered_status, "partial.test")
    assert row["verified"] is False
    assert row["coverage"] == 0.6 and row["coverage_verdict"] == "覆盖率不足"


def test_unverifiable_is_not_verified(layered_status):
    row = _row(layered_status, "unver.test")
    assert row["verified"] is False and row["coverage"] is None
    assert row["coverage_verdict"] == "不可验证"


def test_legacy_anomalous_coverage_not_verified(layered_status):
    """旧快照没有 verdict：coverage 2.1（本地远大于上游）必须按口径异常处理。"""
    row = _row(layered_status, "anomaly.test")
    assert row["verified"] is False and row["coverage"] is None
    assert row["coverage_verdict"] == "口径异常"


def test_needs_config_and_disabled_are_separate(layered_status):
    config = _row(layered_status, "config.test")
    assert config["needs_config"] is True and config["disabled"] is False
    gone = _row(layered_status, "gone.test")
    assert gone["disabled"] is True and gone["needs_config"] is False
    assert gone["disabled_reason"] == "站点已下线"


def test_coverage_merges_sites_and_honors_latest_failed_check(fake_status):
    data = __import__('pathlib').Path(fake_status.DATA)
    _write(data / 'coverage-old.json', {'generated': '2026-11-14T00:00:00Z', 'report': [
        {'domain': 'alpha.test', 'coverage': 1.0, 'verdict': '已核对'},
        {'domain': 'beta.test', 'coverage': .4, 'verdict': '有缺口'}]})
    _write(data / 'coverage-sites/alpha.test.json', {'domain': 'alpha.test', 'checked_at': '2026-11-15T00:00:00Z', 'verdict': '核对失败', 'source': 'HTTP 503'})
    records = fake_status.latest_coverage()
    assert set(records) == {'alpha.test', 'beta.test'}
    assert records['alpha.test']['verdict'] == '核对失败'
    assert fake_status.coverage_flags('alpha.test')['coverage'] is None


def test_expired_or_changed_coverage_is_not_verified(fake_status):
    from pathlib import Path
    from datetime import datetime, timezone
    data = Path(fake_status.DATA)
    record = {'domain': 'alpha.test', 'coverage': 1, 'verdict': '已核对', 'got': {'posts': 1}, 'checked_at': '2020-01-01T00:00:00Z'}
    _write(data / 'coverage-sites/alpha.test.json', record)
    assert fake_status.coverage_flags('alpha.test')['coverage_stale']
    record['checked_at'] = datetime.now(timezone.utc).isoformat()
    _write(data / 'coverage-sites/alpha.test.json', record)
    assert fake_status.coverage_flags('alpha.test')['verified']
    (data / 'alpha.test/urls.txt').write_text('https://alpha.test/a\nhttps://alpha.test/b\n')
    flag = fake_status.coverage_flags('alpha.test')
    assert flag['coverage_stale'] and not flag['verified']


def test_99_percent_with_explicit_gaps_is_not_pass(fake_status):
    from pathlib import Path
    _write(Path(fake_status.DATA) / 'coverage-fresh.json', {'report': [
        {'domain': 'alpha.test', 'coverage': .999, 'verdict': '有缺口', 'crawl_unresolved': {'posts': {'status': 'partial'}}}]})
    flags = fake_status.coverage_flags('alpha.test')
    assert not flags['verified'] and flags['coverage_verdict'] == '覆盖率不足'


def test_legacy_list_report_does_not_break_status(fake_status):
    from pathlib import Path
    _write(Path(fake_status.DATA) / 'coverage-unrelated.json', ['legacy', 'list'])
    assert fake_status.build_status()['sites']
