"""supervisor 任务表改为 sources.yaml 派生（审查问题 4）。"""

import pytest

import supervisor


def _write(tmp_path, text):
    p = tmp_path / "sources.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_tasks_from_sources_and_skips_disabled(tmp_path):
    path = _write(tmp_path, """
- domain: a.test
  channel: sitemap
  base: https://a.test
- domain: b.test
  channel: wp-api
  base: https://b.test
- domain: dead.test
  channel: html
  base: https://dead.test
  disabled: true
  disabled_reason: 站点已下线
""")
    tasks = supervisor.load_tasks(path)
    assert ("a.test", "posts") in tasks
    assert ("b.test", "posts") in tasks and ("b.test", "pages") in tasks and ("b.test", "media") in tasks
    assert all(d != "dead.test" for d, _ in tasks)
    assert len(tasks) == 4


@pytest.mark.parametrize("content", [None, "[]", "- domain: a.test\n  disabled: true"])
def test_empty_catalog_never_creates_tasks(tmp_path, monkeypatch, content):
    monkeypatch.setenv("CRAWL_TASKS", "fixed")  # old deployments may still set this
    path = tmp_path / "sources.yaml"
    if content is not None:
        path.write_text(content)
    assert supervisor.load_tasks(path) == []


def test_local_catalog_overrides_empty_template(tmp_path):
    from crawler.config import sources_path

    template = tmp_path / "sources.example.yaml"
    template.write_text("[]\n")
    assert sources_path(tmp_path) == template
    local = _write(tmp_path, "- domain: site.test\n  channel: rss\n")
    assert sources_path(tmp_path) == local
    assert supervisor.load_tasks(local) == [("site.test", "posts")]
    assert template.read_text() == "[]\n"
