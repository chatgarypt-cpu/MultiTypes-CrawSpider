"""Monitor actions use the authenticated queue and the registered site's own data."""
import json
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
import threading

import pytest
import requests

import serve
import status
import verify_coverage
import web_jobs
import web_worker
from crawler import runner, store
from crawler.request import RequestQueue, canonical_key


@pytest.fixture
def monitor(tmp_env, monkeypatch):
    site = {"domain": "news.test", "base": "https://news.test", "channel": "sitemap"}
    sites = {site["domain"]: site, "closed.test": dict(site, domain="closed.test", disabled=True)}
    monkeypatch.setattr(status, "source_cfg", lambda: sites)
    monkeypatch.setattr(web_jobs, "DATA", str(tmp_env))
    monkeypatch.setattr(web_jobs, "JOBS", tmp_env / "web-jobs")
    monkeypatch.setattr(web_jobs, "PROCS", {})
    monkeypatch.setattr(web_jobs, "STOPPING", {})
    monkeypatch.setattr(verify_coverage, "DATA", str(tmp_env))
    return site


def test_site_action_route_authorizes_validates_and_deduplicates(monitor, tmp_env, monkeypatch):
    monkeypatch.setenv("CRAWL_WEB_PASSWORD", "test-password")
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.H)
    server.desktop = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/site-actions"
    auth = ("admin", "test-password")
    verify = {"action": "verify", "domain": monitor["domain"]}
    backfill = {**verify, "action": "backfill", "mode": "resume", "kinds": ["posts"]}
    try:
        assert requests.post(url, json=verify, timeout=5).status_code == 401
        assert requests.post(url, auth=auth, json=verify, timeout=5,
                             headers={"Origin": "https://unrelated.test"}).status_code == 403
        for bad in [dict(verify, domain="closed.test"), dict(verify, domain="../data"),
                    dict(backfill, kinds=["media"]), dict(backfill, mode="clear")]:
            assert requests.post(url, auth=auth, json=bad, timeout=5).status_code == 400
        (tmp_env / "CRAWL_SKIP").write_text("news.test:posts\n")
        assert requests.post(url, auth=auth, json=backfill, timeout=5).status_code == 400
        response = requests.post(url, auth=auth, json=verify, timeout=5)
        assert response.status_code == 202
        first = response.json()
        assert first["job"]["status"] == "queued"
        (tmp_env / "CRAWL_SKIP").unlink()
        conflict = requests.post(url, auth=auth, json=backfill, timeout=5).json()
        assert conflict["duplicate"] is True and conflict["job"]["id"] == first["job"]["id"]
        assert web_jobs.latest_site_actions(web_jobs.jobs())["news.test"]["verify"] == first["job"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_monitor_worker_targets_canonical_data_and_leaves_normal_queue_isolated(monitor, tmp_env, monkeypatch):
    job = web_jobs.submit_site_action({"action": "verify", "domain": monitor["domain"]})["job"]
    monkeypatch.setattr(web_jobs, "normalize_url", lambda value: value)
    plain = web_jobs.submit([monitor["base"]])[0]
    launches = []

    def start(command, **kwargs):
        launches.append((command[-1], kwargs["env"]["CRAWL_DATA_DIR"]))
        return SimpleNamespace(pid=len(launches), poll=lambda: None)

    monkeypatch.setattr(web_jobs, "subprocess", SimpleNamespace(Popen=start, STDOUT=-2))
    web_jobs.tick()
    assert dict(launches) == {
        str(web_jobs.job_dir(job["id"])): str(tmp_env),
        str(web_jobs.job_dir(plain["id"])): str(web_jobs.job_dir(plain["id"]) / "data"),
    }


def test_resumed_action_result_stays_visible_after_completion(monitor):
    request = {"action": "verify", "domain": monitor["domain"]}
    first = web_jobs.submit_site_action(request)["job"]
    first_path = web_jobs.job_dir(first["id"]) / "job.json"
    web_jobs.write(first_path, dict(web_jobs.read(first_path), status="failed"))
    second = web_jobs.submit_site_action(request)["job"]
    second_path = web_jobs.job_dir(second["id"]) / "job.json"
    web_jobs.write(second_path, dict(web_jobs.read(second_path), status="finished"))

    web_jobs.resume(first["id"])
    web_jobs.write(first_path, dict(web_jobs.read(first_path), status="finished"))

    latest = web_jobs.latest_site_actions(web_jobs.jobs())[monitor["domain"]]["verify"]
    assert latest["id"] == first["id"]


def test_batch_queues_each_eligible_site_and_reports_duplicates_and_errors(monitor, tmp_env, monkeypatch):
    wp = dict(monitor, domain="wp.test", channel="wp-api")
    paused = dict(monitor, domain="paused.test")
    sites = {site["domain"]: site for site in (monitor, wp, paused)}
    monkeypatch.setattr(status, "source_cfg", lambda: sites)
    (tmp_env / "queue_config.json").write_text('{"concurrency": 1}')
    (tmp_env / "CRAWL_SKIP").write_text("paused.test:posts\n")

    request = {"action": "backfill", "mode": "resume", "targets": [
        {"domain": "news.test", "kinds": ["posts"]},
        {"domain": "wp.test", "kinds": ["pages", "media"]},
        {"domain": "paused.test", "kinds": ["posts"]},
        {"domain": "news.test", "kinds": ["posts"]},
    ]}
    first, second, rejected, duplicate = web_jobs.submit_site_action(request)["results"]

    assert first["job"]["status"] == second["job"]["status"] == "queued"
    assert rejected["domain"] == "paused.test" and "暂停" in rejected["error"]
    assert duplicate["duplicate"] and duplicate["job"]["id"] == first["job"]["id"]
    queue = {job["domain"]: job["kinds"] for job in web_jobs.jobs()}
    assert queue == {"news.test": ["posts"], "wp.test": ["pages", "media"]}


def test_refresh_writes_coverage_without_altering_collection_files(monitor, tmp_env, monkeypatch):
    domain = monitor["domain"]
    store.append_rows(domain, [{"url": f"https://{domain}/known", "title": "Known"}])
    data_file = tmp_env / domain / "articles.jsonl"
    before = data_file.read_bytes()
    monkeypatch.setattr(verify_coverage, "verify_site", lambda site, got: {
        "domain": site["domain"], "got": got, "verdict": "已核对", "checked_at": "2026-11-25T00:00:00Z",
    })
    receipt = web_jobs.submit_site_action({"action": "verify", "domain": domain})["job"]
    job = web_jobs.read(web_jobs.job_dir(receipt["id"]) / "job.json")
    web_worker.run_site_action(job, lambda **changes: job.update(changes))
    record = json.loads((tmp_env / "coverage-sites" / f"{domain}.json").read_text())
    assert record["got"] == {"posts": 1}
    assert data_file.read_bytes() == before
    assert job["status"] == "finished"


def test_backfill_aggregates_identical_errors_once_but_keeps_each_failed_collection(monitor, monkeypatch):
    monitor["channel"] = "wp-api"
    error = "first page 404 missing"
    kinds = ["posts", "pages", "media"]
    monkeypatch.setattr(runner, "collect_site", lambda *_args, **_kwargs: {"status": "interrupted", "error": error})
    request = {"action": "backfill", "domain": monitor["domain"], "mode": "resume", "kinds": kinds}
    receipt = web_jobs.submit_site_action(request)["job"]
    job = web_jobs.read(web_jobs.job_dir(receipt["id"]) / "job.json")

    web_worker.run_site_action(job, lambda **changes: job.update(changes))

    assert job["status"] == "failed"
    assert job["error"] == error
    assert {kind: result["error"] for kind, result in job["results"].items()} == dict.fromkeys(kinds, error)


def test_retry_backfill_reopens_recoverable_rows_and_preserves_dead_links(monitor, tmp_env, monkeypatch):
    domain = monitor["domain"]
    base = monitor["base"]
    q = RequestQueue(domain)
    q.add_many([f"{base}/{part}" for part in ("temporary", "empty", "gone", "oversize")])
    q.mark_error([canonical_key(f"{base}/temporary")], {f"{base}/temporary": "http_503 down"}, max_retries=1)
    q.mark_error([canonical_key(f"{base}/gone")], {f"{base}/gone": "http_404 gone"}, max_retries=1)
    q.mark_error([canonical_key(f"{base}/oversize")], {f"{base}/oversize": "http_0 response_size_limit"})
    q.mark_skipped([canonical_key(f"{base}/empty")], "no_text")
    store.append_rows(domain, [{"url": f"{base}/known", "title": "Saved"}])

    def collect(site, limit, kind, **opts):
        assert site == monitor and limit == 0 and kind == "posts" and opts == {"export_csv": False}
        assert {record.url for record in q.next_batch()} == {f"{base}/temporary", f"{base}/empty"}
        assert store.load_known(domain) == {f"{base}/known"}
        return {"status": "complete", "saved": 0}

    monkeypatch.setattr(runner, "collect_site", collect)
    result = web_jobs.submit_site_action({"action": "backfill", "domain": domain, "mode": "retry", "kinds": ["posts"]})
    path = web_jobs.job_dir(result["job"]["id"]) / "job.json"
    job = web_jobs.read(path)
    web_worker.run_site_action(job, lambda **changes: job.update(changes))
    assert job["status"] == "finished"
    assert job["results"]["posts"]["requeued"] == 2
    assert q.stats().get("error") == 2


def test_rescan_restarts_both_json_list_checkpoints_but_keeps_existing_data(monitor, tmp_env, monkeypatch):
    domain = monitor["domain"]
    monitor["channel"] = "json-list"
    store.append_rows(domain, [{"url": "https://news.test/known"}])
    directory = tmp_env / domain
    for name in ("cursor.json", "jsonlist.cursor.json"):
        (directory / name).write_text("{}")

    def collect(*_args, **_kwargs):
        assert not (directory / "cursor.json").exists()
        assert not (directory / "jsonlist.cursor.json").exists()
        assert store.load_known(domain) == {"https://news.test/known"}
        return {"status": "partial", "saved": 0}

    monkeypatch.setattr(runner, "collect_site", collect)
    result = web_jobs.submit_site_action({"action": "backfill", "domain": domain, "mode": "rescan", "kinds": ["posts"]})
    job = web_jobs.read(web_jobs.job_dir(result["job"]["id"]) / "job.json")
    web_worker.run_site_action(job, lambda **changes: job.update(changes))
    assert job["status"] == "partial"

    # A user resumes the interrupted scan. Its new checkpoint must survive.
    (directory / "jsonlist.cursor.json").write_text('{"page": 3}')
    monkeypatch.setattr(runner, "collect_site", lambda *_args, **_kwargs: {
        "status": "complete", "saved": int((directory / "jsonlist.cursor.json").exists()),
    })
    web_worker.run_site_action(job, lambda **changes: job.update(changes))
    assert job["status"] == "finished" and job["saved"] == 1
