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
