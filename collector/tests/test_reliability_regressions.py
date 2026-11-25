"""Regression cases for failure handling and restart safety, using isolated storage."""
import json
import logging
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import weakref
from types import SimpleNamespace

import pytest

import collect
import run_queue
import supervisor
import web_jobs
from crawler import cursor, paywall, store, web
from crawler.channels import base, json_list, wp_api
from crawler.request import RequestQueue, canonical_key

COLLECTOR = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)


def child(code, data, *args):
    return [sys.executable, "-c", code, str(data), *args]


CHILD_IMPORTS = f"""
import os, sys
os.environ["CRAWL_DATA_DIR"] = sys.argv[1]
sys.path.insert(0, {str(COLLECTOR)!r})
from crawler import store
"""


@pytest.mark.parametrize("crash_on", ["articles.jsonl", "urls.txt"])
def test_append_recovers_after_process_dies_without_duplicating(tmp_env, crash_on):
    domain, url = "journal.test", "https://journal.test/2"
    store.append_rows(domain, [{"url": "https://journal.test/1"}])
    queue = RequestQueue(domain)
    queue.add_many([url])
    code = CHILD_IMPORTS + """
original = store._append_lines
def crash(path, lines):
    original(path, lines)
    if path.endswith(sys.argv[2]):
        os._exit(87)
store._append_lines = crash
store.append_rows("journal.test", [{"url": "https://journal.test/2"}])
"""
    result = subprocess.run(child(code, tmp_env, crash_on), timeout=10, check=False)
    assert result.returncode == 87
    assert (tmp_env / domain / "urls.txt.append").is_file()
    known = store.load_known(domain)
    if crash_on == "articles.jsonl":
        assert url not in known
        assert store.append_rows(domain, [{"url": url}]) == 1
    else:
        assert url in known
    assert store.append_rows(domain, [{"url": url}]) == 0
    queue.seed_done(store.load_known(domain))
    assert queue.pending() == 0
    assert queue.stats()["done"] == 2
    assert len((tmp_env / domain / "articles.jsonl").read_text().splitlines()) == 2


def test_site_lease_refuses_second_runner_and_releases_on_exit(tmp_env):
    code = CHILD_IMPORTS + """
try:
    with store.collection_lock("locked.test", wait=False):
        store.append_rows("locked.test", [{"url": "https://locked.test/b"}])
except store.CollectionBusy:
    sys.exit(75)
"""
    with store.collection_lock("locked.test"):
        store.append_rows("locked.test", [{"url": "https://locked.test/a"}])
        busy = subprocess.run(child(code, tmp_env), timeout=10, check=False)
        assert busy.returncode == 75
    subprocess.run(child(code, tmp_env), timeout=10, check=True)
    assert store.load_known("locked.test") == {"https://locked.test/a", "https://locked.test/b"}


def test_rollback_cannot_truncate_concurrent_writer_output(tmp_env):
    domain = "overlap.test"
    store.append_rows(domain, [{"url": "https://overlap.test/prior"}])
    fail_code = CHILD_IMPORTS + """
original = store._append_lines
def fail(path, lines):
    if path.endswith("urls.txt"):
        print("about to roll back", flush=True)
        sys.stdin.readline()
        raise OSError("cannot append URL index")
    original(path, lines)
store._append_lines = fail
try:
    store.append_rows("overlap.test", [{"url": "https://overlap.test/aborted"}])
except OSError:
    sys.exit(86)
"""
    save_code = CHILD_IMPORTS + """
print("ready", flush=True)
assert store.append_rows("overlap.test", [{"url": "https://overlap.test/saved"}]) == 1
"""
    a = subprocess.Popen(child(fail_code, tmp_env), stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True)
    b = None
    try:
        assert select.select([a.stdout], [], [], 10)[0]
        assert a.stdout.readline().strip() == "about to roll back"
        b = subprocess.Popen(child(save_code, tmp_env), stdout=subprocess.PIPE, text=True)
        assert select.select([b.stdout], [], [], 10)[0]
        assert b.stdout.readline().strip() == "ready"
        assert b.poll() is None
        a.stdin.write("\n")
        a.stdin.flush()
        assert a.wait(timeout=10) == 86
        assert b.wait(timeout=10) == 0
    finally:
        for process in (a, b):
            if process:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                process.stdout.close()
                if process.stdin:
                    process.stdin.close()
    assert store.load_known(domain) == {"https://overlap.test/prior", "https://overlap.test/saved"}
    rows = [json.loads(line)["url"] for line in (tmp_env / domain / "articles.jsonl").read_text().splitlines()]
    assert rows == ["https://overlap.test/prior", "https://overlap.test/saved"]


def test_legacy_queue_leaves_non_wordpress_store_in_place(tmp_env, monkeypatch):
    domain = "legacy.test"
    store.append_rows(domain, [{"url": "https://legacy.test/a"}])
    cursor.save(domain, status="partial", saved=1)
    directory = tmp_env / domain
    originals = {name: (directory / name).read_bytes() for name in ("articles.jsonl", "urls.txt", "cursor.json")}

    def launch(*_args, **_kwargs):
        assert {name: (directory / name).read_bytes() for name in originals} == originals
        return SimpleNamespace(poll=lambda: 0, returncode=0)

    monkeypatch.setattr(run_queue, "DATA", str(tmp_env))
    monkeypatch.setattr(run_queue, "say", lambda *_: None)
    monkeypatch.setattr(run_queue, "build_tasks", lambda: [(domain, "posts", {"channel": "sitemap"})])
    monkeypatch.setattr(run_queue, "load_blocked", set)
    monkeypatch.setattr(run_queue, "load_done", set)
    monkeypatch.setattr(run_queue, "save_done", lambda _: None)
    monkeypatch.setattr(run_queue, "live", lambda _: False)
    monkeypatch.setattr(run_queue, "subprocess", SimpleNamespace(Popen=launch, STDOUT=subprocess.STDOUT))
    monkeypatch.setattr(run_queue, "time", SimpleNamespace(sleep=lambda _: None))
    run_queue.main()
    assert not (directory / ".rebuilt").exists()


def test_supervisor_resume_logs_and_retry_budget(tmp_env, monkeypatch):
    keys = [(name, "posts") for name in ("paused.test", "progress.test", "fail.test", "idle.test")]
    skip = tmp_env / "CRAWL_SKIP"
    skip.write_text("paused.test:posts\n")
    clock, launched, stopped, handlers = [100], [], [], {}
    process_info = {}

    class Proc:
        pid = 10000
        rc = None

        def poll(self):
            return self.rc

    def start(key, running):
        dom, _ = key
        launched.append(dom)
        logfile = tmp_env / f"{dom}.log"
        logfile.write_text("start")
        proc = Proc()
        if dom == "fail.test":
            proc.rc = 1
        elif dom != "progress.test":
            proc.rc = 0
            cursor.save(dom, status="partial" if dom == "idle.test" else "complete",
                        last_reason="no_output" if dom == "idle.test" else "nothing_new")
        running[key] = process_info[dom] = {
            "proc": proc, "start": clock[0], "before": 0, "initial_cursor": {},
            "fh": SimpleNamespace(close=lambda: None), "logfile": logfile,
            "progress": supervisor.progress_signature(tmp_env, key, logfile), "last_progress": clock[0],
        }

    def sleep(_):
        clock[0] += 10
        if clock[0] == 110:
            skip.unlink()
        if clock[0] <= 130:
            with process_info["progress.test"]["logfile"].open("a") as handle:
                handle.write(" fetched")
        if clock[0] == 140:
            cursor.save("progress.test", status="complete")
            process_info["progress.test"]["proc"].rc = 0
        if clock[0] == 180:
            handlers[signal.SIGTERM]()

    for name, value in {
        "DATA": tmp_env, "PAUSE": tmp_env / "CRAWL_PAUSED", "SKIP": skip,
        "SOURCES": tmp_env / "sources.yaml", "MAX_CONCURRENT": 4,
        "STALL_KILL": 8, "RETRY_DELAY": 0, "MAX_ATTEMPTS": 2, "STATE_EVERY": 0,
        "load_tasks": lambda: keys, "start": start,
        "_write_health": lambda *_: None, "log": lambda *_: None,
        "signal_group": lambda *_: None, "stop_group": lambda proc: stopped.append(proc),
        "signal": SimpleNamespace(SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT, SIGKILL=signal.SIGKILL,
                                  signal=lambda sig, handler: handlers.update({sig: handler})),
        "time": SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0], sleep=sleep,
                                strftime=time.strftime),
    }.items():
        monkeypatch.setattr(supervisor, name, value)
    states = []
    monkeypatch.setattr(supervisor, "write_state", states.append)
    assert supervisor.main() == 0
    assert states[0]["paused_by_user"] == ["paused.test:posts"]
    assert not states[-1]["paused_by_user"]
    assert sorted(states[-1]["completed"]) == ["paused.test:posts", "progress.test:posts"]
    assert states[-1]["stalled"] == ["fail.test:posts"]
    assert states[-1]["idle"] == ["idle.test:posts"]
    assert launched.count("fail.test") == 2 and launched.count("idle.test") == 1
    assert not stopped


def test_web_watchdog_frees_a_slot_after_stopping_worker(tmp_env, monkeypatch):
    monkeypatch.setattr(web_jobs, "DATA", str(tmp_env))
    monkeypatch.setattr(web_jobs, "JOBS", tmp_env / "jobs")
    monkeypatch.setattr(web_jobs, "PROCS", {})
    monkeypatch.setattr(web_jobs, "STOPPING", {})
    monkeypatch.setattr(web_jobs, "STALL_KILL", 1)
    old, queued = "a" * 32, "b" * 32
    start = time.time() - 3600
    for jobid, state in [(old, "running"), (queued, "queued")]:
        web_jobs.write(web_jobs.job_dir(jobid) / "job.json", {
            "id": jobid, "url": f"https://{jobid}.test", "status": state,
            "created": start, "started_epoch": start,
        })
    web_jobs.write(tmp_env / "queue_config.json", {"concurrency": 1})
    results, old_exit = [], [None]
    web_jobs.PROCS[old] = SimpleNamespace(poll=lambda: old_exit[0], pid=43210)

    def signal_worker(pid, sig):
        if sig == signal.SIGTERM:
            assert pid == 43210
            old_exit[0] = -sig

    monkeypatch.setattr(web_jobs, "signal_group", signal_worker)
    monkeypatch.setattr(web_jobs, "held", lambda _: old_exit[0] is None)
    monkeypatch.setattr(web_jobs, "subprocess", SimpleNamespace(
        Popen=lambda cmd, **_: results.append(cmd) or SimpleNamespace(pid=9876, poll=lambda: None),
        STDOUT=subprocess.STDOUT))
    web_jobs.tick()
    assert not results
    web_jobs.tick()
    assert len(results) == 1 and results[0][-1] == str(web_jobs.job_dir(queued))
    assert web_jobs.read(web_jobs.job_dir(old) / "job.json")["status"] == "interrupted"


def test_oversize_curl_aborts_at_callback_and_queue_leaves_it_terminal(tmp_env, monkeypatch):
    calls = []

    def get(_url, *, content_callback, **_):
        calls.append(1)
        assert content_callback(b"%PDF-") == 5
        assert content_callback(b"x" * 5) == web.CURL_WRITEFUNC_ERROR
        raise OSError("curl write aborted")

    monkeypatch.setattr(web, "MAX_BODY_BYTES", 8)
    monkeypatch.setattr(web.cffi_requests, "get", get)
    fetcher = web.Fetcher({"domain": "pdf.test", "via": "impersonate", "documents": True})
    url = "https://pdf.test/a"
    assert fetcher._impersonate_text(url, {}) == (0, "response_size_limit")
    assert len(calls) == 1

    q = RequestQueue("pdf.test")
    q.add_many([url])
    key = canonical_key(url)
    q.mark_error([key], {key: "http_0 response_size_limit"})
    assert q.stats() == {"error": 1}
    assert q.retry_transient(cooldown=0, statuses=(0,)) == 0


def test_interrupted_response_is_retried_and_never_returned_as_200(monkeypatch):
    from test_fetch import FakeResponse

    class Incomplete(FakeResponse):
        def iter_content(self, *_):
            yield b"<html><p>part of the article</p>"
            raise web.requests.exceptions.ChunkedEncodingError("incomplete body")

    responses = iter([Incomplete(), FakeResponse(body=b"complete")])
    monkeypatch.setattr(web.SESSION, "get", lambda *_, **__: next(responses))
    monkeypatch.setattr(web, "_sleep_backoff", lambda _: None)
    assert web.get_text("https://incomplete.test/", {}) == (200, "complete")


def test_stream_releases_consumed_response_before_the_batch_finishes(monkeypatch):
    class Body:
        pass

    fetcher = web.Fetcher({"domain": "memory.test"})
    monkeypatch.setattr(fetcher, "_fetch_one", lambda url: (url, 200, Body()))
    results = fetcher.stream_many([f"https://memory.test/{i}" for i in range(8)], workers=2)
    first = weakref.ref(next(results)[2])
    next(results)
    assert first() is None
    results.close()


@pytest.mark.parametrize("body", ["null", '{"archived_snapshots":[]}', '{"archived_snapshots":{"closest":true}}'])
def test_malformed_archive_shape_is_isolated(monkeypatch, body):
    monkeypatch.setattr(paywall, "_get", lambda *_, **__: (200, body, ""))
    assert paywall._archive_org("https://example.test/a") == (0, "", "")


def test_nothing_pending_keeps_skipped_rows_incomplete(tmp_env):
    q = RequestQueue("coverage.test")
    missing, skipped = "https://coverage.test/missing", "https://coverage.test/skipped"
    q.add_many([missing, skipped])
    key = canonical_key(missing)
    q.mark_error([key], {key: "http_404 gone"}, max_retries=1)
    q.mark_skipped([canonical_key(skipped)], "no_text")
    cursor.save("coverage.test", status="partial")
    result = base.collect_urls({"domain": "coverage.test", "channel": "sitemap"},
                               [missing, skipped], 0, LOGGER, None)
    assert result["status"] == "partial"
    assert cursor.load("coverage.test")["queue"] == {"error": 1, "skipped": 1}


def test_zero_batch_reports_bad_config_without_empty_spin(tmp_env, monkeypatch):
    site = {"domain": "json.test", "base": "https://json.test", "channel": "json-list"}
    RequestQueue(site["domain"]).add_many(["https://json.test/a"])
    monkeypatch.setenv("QUEUE_BATCH", "0")
    with pytest.raises(ValueError, match="QUEUE_BATCH"):
        json_list.collect_json_list(site)


@pytest.mark.parametrize("via", ["direct", "playwright"])
def test_wordpress_repeated_pages_stop_with_resume_cursor(tmp_env, monkeypatch, via):
    domain = "wp-repeat.test"
    site = {"domain": domain, "base": f"https://{domain}", "channel": "wp-api", "via": via}
    response = json.dumps([{"id": 1, "link": f"https://{domain}/entry", "title": {"rendered": "Entry"}}])

    def get(*_, **__):
        return 200, response, {}
    monkeypatch.setattr(wp_api, "_get_for_site", get)
    stub = SimpleNamespace(get_many=lambda urls, **_: [(url, 200, response) for url in urls])
    instance = wp_api.collect_wp(site, workers=1, progress=False) if via == "direct" else wp_api.run(site, 0, LOGGER, stub)
    assert instance["status"] == "interrupted"
    assert cursor.load(domain)["last_reason"] == "repeated_page"
    assert cursor.load(domain)["next_page"] == 2
    assert store.load_known(domain) == {f"https://{domain}/entry"}


def test_cli_collect_error_and_unknown_site_fail(tmp_env, monkeypatch):
    source = tmp_env / "sources.yaml"
    source.write_text("- domain: test.test\n  base: https://test.test\n  channel: sitemap\n")
    monkeypatch.setattr(collect, "sources_path", lambda *_: source)
    monkeypatch.setattr(sys, "argv", ["collect.py", "--site", "test.test"])

    def fail(*_, **__):
        raise OSError("write failed")
    monkeypatch.setattr(collect, "collect_site", fail)
    assert collect.main() == 1
    monkeypatch.setattr(sys, "argv", ["collect.py", "--site", "unknown.test"])
    with pytest.raises(SystemExit) as err:
        collect.main()
    assert err.value.code != 0
