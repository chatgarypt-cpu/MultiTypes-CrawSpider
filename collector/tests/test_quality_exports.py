"""Storage, seven-day quality metrics, and the CSV download contract."""
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import export_csv
from crawler import runner
from crawler.channels import base
from crawler.quality import site_quality


def write_article(path, number):
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"url": f"https://site.test/{number}", "title": str(number)}) + "\n")


def test_download_refresh_is_atomic_and_reuses_current_csv(tmp_path, monkeypatch):
    site = tmp_path / "site.test"
    site.mkdir()
    source = site / "articles.jsonl"
    write_article(source, 1)
    export_csv.refresh_download(tmp_path, "site.test.csv")
    target = tmp_path / "csv/site.test.csv"
    before = target.read_bytes()
    inode = target.stat().st_ino
    export_csv.refresh_download(tmp_path, target.name)
    assert target.stat().st_ino == inode
    write_article(source, 2)
    with monkeypatch.context() as patch:
        patch.setattr(export_csv, "stream", lambda *_: (_ for _ in ()).throw(OSError("read failed")))
        with pytest.raises(OSError):
            export_csv.refresh_download(tmp_path, target.name)
    assert target.read_bytes() == before
    export_csv.refresh_download(tmp_path, target.name)
    assert b"https://site.test/2" in target.read_bytes()
    last_good = target.read_bytes()
    with source.open("a", encoding="utf-8") as out:
        out.write("{broken}\n")
    with pytest.raises(ValueError, match="invalid JSON record"):
        export_csv.refresh_download(tmp_path, target.name)
    assert target.read_bytes() == last_good


def test_background_export_switch_and_legacy_notes(tmp_env, monkeypatch):
    site = {"domain": "a.test", "channel": "rss", "base": "https://a.test", "feed": "/feed"}
    called = []
    monkeypatch.setattr(runner, "_export", lambda *args: called.append(args))
    responses = iter([
        {"status": "partial", "saved": 1, "reasons": {"empty": 999}, "quality": {
            "version": 1, "discovered": 3, "attempted": 4,
            "request_failures": {"http_503": 2}, "rejections": {"short_text": 1}}},
        {"status": "complete", "saved": 0, "reasons": {"empty": 999}},
    ])
    monkeypatch.setitem(runner.CHANNELS, "rss", lambda *args: next(responses))
    runner.collect_site(site, 0, export_csv=False)
    assert not called
    runner.collect_site(site, 0)
    assert len(called) == 1
    now = datetime.now(timezone.utc)
    db = tmp_env / "state.db"
    with sqlite3.connect(db) as conn:
        for days in (1, 9):
            conn.execute("INSERT INTO runs(domain,finished,saved,note) VALUES(?,?,?,?)",
                         ("a.test", (now - timedelta(days=days)).isoformat(), 5, '{"reasons":{"empty":999}}'))
    row, = site_quality(db, now)["sites"]
    assert (row["runs"], row["measured_runs"], row["saved"]) == (3, 1, 6)
    assert (row["discovered"], row["attempted"], row["request_failed"], row["rejected"]) == (3, 4, 2, 1)
    assert row["fetched"] == 2
    assert row["incomplete_runs"] == 1
    assert row["acceptance"] == .25
    assert row["top_reasons"] == [("http_503", 2), ("short_text", 1)]


def test_detail_retries_count_only_in_current_run(tmp_env):
    class Failing:
        def stream_requests(self, batch, workers):
            for req in batch:
                yield req, 500, "temporary"

    site = {"channel": "html", "domain": "a.test"}
    urls = ["https://a.test/story"]
    args = (site, urls, 0, logging.getLogger(__name__), Failing())
    first = base.collect_urls(*args)
    again = base.collect_urls(*args)
    assert first["quality"]["attempted"] > 0
    assert first["quality"]["request_failures"] == {"http_500": first["quality"]["attempted"]}
    assert again["quality"]["attempted"] == 0
    assert again["quality"]["request_failures"] == {}
