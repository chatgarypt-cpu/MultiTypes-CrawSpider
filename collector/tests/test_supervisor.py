"""守护进程（collector/supervisor.py）测试：只测纯函数/状态读写，绝不真起子进程。"""
import json
import time
from pathlib import Path

import supervisor


def make_site(root: Path, domain: str, kind: str = "posts", rows: int = 3,
              done: bool = False) -> None:
    d = root / domain
    d.mkdir(parents=True, exist_ok=True)
    name = "urls.txt" if kind == "posts" else f"{kind}.urls.txt"
    (d / name).write_text("".join(f"https://{domain}/{i}\n" for i in range(rows)),
                          encoding="utf-8")
    cursor = "cursor.json" if kind == "posts" else f"{kind}.cursor.json"
    (d / cursor).write_text(json.dumps({"done": done, "next_page": 1}),
                            encoding="utf-8")


def test_read_skip(tmp_path):
    skip = tmp_path / "CRAWL_SKIP"
    skip.write_text("# 注释\n\nwww.a.test:posts\nwww.b.test:media\n",
                    encoding="utf-8")
    assert supervisor.read_skip(skip) == {"www.a.test:posts", "www.b.test:media"}
    assert supervisor.read_skip(tmp_path / "missing") == set()


def test_rows_and_is_done(tmp_path):
    make_site(tmp_path, "a.test", "posts", rows=4, done=True)
    make_site(tmp_path, "a.test", "media", rows=2, done=False)
    assert supervisor.rows(tmp_path, "a.test") == 4
    assert supervisor.rows(tmp_path, "a.test", "media") == 2
    assert supervisor.is_done(tmp_path, "a.test") is True
    assert supervisor.is_done(tmp_path, "a.test", "media") is False
    # 不存在的站点/文件
    assert supervisor.rows(tmp_path, "nope.test") == 0
    assert supervisor.is_done(tmp_path, "nope.test") is False


def test_is_done_corrupt_cursor(tmp_path):
    d = tmp_path / "bad.test"
    d.mkdir()
    (d / "cursor.json").write_text("{not json", encoding="utf-8")
    assert supervisor.is_done(tmp_path, "bad.test") is False


def test_classify_exit():
    assert supervisor.classify_exit(True, 0, max_attempts=3) == ("completed", 0)
    assert supervisor.classify_exit(False, 0, max_attempts=3) == ("retry", 1)
    assert supervisor.classify_exit(False, 1, max_attempts=3) == ("retry", 2)
    assert supervisor.classify_exit(False, 2, max_attempts=3) == ("stalled", 3)


def test_watchdog_action():
    now = 10000.0
    # 还没到 120s 检查点
    assert supervisor.watchdog_action(now, now - 60, 100, 100, stall_kill=1200) is None
    # 有增长 -> progress
    assert supervisor.watchdog_action(now, now - 200, 100, 110, stall_kill=1200) == "progress"
    # 无增长但未超时 -> wait
    assert supervisor.watchdog_action(now, now - 200, 100, 100, stall_kill=1200) == "wait"
    # 无增长超过 20 分钟 -> kill
    assert supervisor.watchdog_action(now, now - 1300, 100, 100, stall_kill=1200) == "kill"
    # 即使超过 20 分钟，只要行数有增长就不杀
    assert supervisor.watchdog_action(now, now - 1300, 100, 101, stall_kill=1200) == "progress"


def test_state_payload(tmp_path):
    make_site(tmp_path, "a.test", "posts", rows=10)
    now = time.time()
    running = {
        ("a.test", "posts"): {"start": now - 30, "before": 4,
                              "last_rows": 4, "last_progress": now - 5},
    }
    queue = [("b.test", "posts"), ("c.test", "media")]
    payload = supervisor.state_payload(
        tmp_path, running, queue, completed=[("d.test", "posts")],
        stalled=[("e.test", "posts")],
        attempts={("e.test", "posts"): 3}, now=now)

    assert payload["updated"]
    run = payload["running"][0]
    assert run["domain"] == "a.test" and run["kind"] == "posts"
    assert run["rows"] == 10
    assert run["delta"] == 6                       # 10 - before(4)
    assert run["elapsed_sec"] == 30
    assert run["log"] == "data/crawl-a.test-posts.log"
    assert payload["pending"] == ["b.test:posts", "c.test:media"]
    assert payload["completed"] == ["d.test:posts"]
    assert payload["stalled"] == ["e.test:posts"]
    assert payload["attempts"] == {"e.test:posts": 3}


def test_write_state_atomic(tmp_path):
    path = tmp_path / "supervise_state.json"
    payload = {"updated": "2026-11-07T00:00:00", "running": [], "pending": []}
    written = supervisor.write_state(payload, path=path)
    assert written == payload
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert not list(tmp_path.glob("*.tmp"))

    # 也支持 write_state(path=..., **字段)
    supervisor.write_state(path=path, updated="2026-11-07T01:00:00", running=[])
    assert json.loads(path.read_text(encoding="utf-8"))["updated"] == "2026-11-07T01:00:00"


def test_tasks_queue_dedup():
    # TASKS 里保留历史顺序（可能有重复），main() 用 dict.fromkeys 去重
    queue = list(dict.fromkeys(supervisor.TASKS))
    assert len(queue) == len(set(queue))
    assert all(len(key) == 2 and key[1] in ("posts", "pages", "media")
               for key in queue)


def test_health_write_integration(tmp_path):
    """_write_health 应写出 data/health.json（不涉及子进程）。"""
    make_site(tmp_path, "a.test", "posts", rows=3)
    supervisor._write_health(tmp_path, {("a.test", "posts"): {}},
                             queue=[("b.test", "posts")], stalled=[], completed=[])
    payload = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert payload["running"] == ["a.test:posts"]
    assert payload["pending"] == 1
    assert payload["total_rows"] == 3
