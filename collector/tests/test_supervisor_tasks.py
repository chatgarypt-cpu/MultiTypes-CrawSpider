"""supervisor 任务表改为 sources.yaml 派生（审查问题 4）。"""
import sys

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


def test_load_tasks_falls_back_to_fixed_when_sources_missing(tmp_path):
    tasks = supervisor.load_tasks(tmp_path / "nope.yaml")
    assert tasks == list(supervisor.TASKS)
    assert tasks


def test_load_tasks_fixed_override(tmp_path, monkeypatch):
    path = _write(tmp_path, "- domain: a.test\n  channel: sitemap\n  base: https://a.test\n")
    monkeypatch.setenv("CRAWL_TASKS", "fixed")
    assert supervisor.load_tasks(path) == list(supervisor.TASKS)
