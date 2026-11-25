"""全局放开开关（data/CRAWL_BYPASS）：覆盖 robots 与 disabled 两处默认策略。"""
import yaml

import supervisor
from crawler import config, web


def test_bypass_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA", str(tmp_path))
    monkeypatch.delenv("CRAWL_BYPASS", raising=False)
    assert config.bypass() is False


def test_bypass_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA", str(tmp_path))
    monkeypatch.setenv("CRAWL_BYPASS", "1")
    assert config.bypass() is True


def test_bypass_sentinel_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA", str(tmp_path))
    monkeypatch.delenv("CRAWL_BYPASS", raising=False)
    (tmp_path / "CRAWL_BYPASS").write_text("")
    assert config.bypass() is True


def test_fetcher_ignores_robots_under_bypass(monkeypatch):
    monkeypatch.setattr(web, "bypass", lambda: True)
    fetcher = web.Fetcher({"domain": "x.test", "base": "https://x.test"})
    assert fetcher.robots is False


def test_fetcher_keeps_robots_without_bypass(monkeypatch):
    monkeypatch.setattr(web, "bypass", lambda: False)
    fetcher = web.Fetcher({"domain": "x.test", "base": "https://x.test"})
    assert fetcher.robots is True


def test_supervisor_queues_disabled_under_bypass(monkeypatch, tmp_path):
    catalog = tmp_path / "sources.yaml"
    catalog.write_text(yaml.safe_dump([
        {"domain": "a.test", "channel": "html", "base": "https://a.test", "disabled": True},
        {"domain": "b.test", "channel": "html", "base": "https://b.test"},
    ]), encoding="utf-8")
    monkeypatch.setattr(supervisor.config, "bypass", lambda: True)
    assert ("a.test", "posts") in supervisor.load_tasks(catalog)

    monkeypatch.setattr(supervisor.config, "bypass", lambda: False)
    assert ("a.test", "posts") not in supervisor.load_tasks(catalog)
