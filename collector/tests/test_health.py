"""健康快照（collector/health.py）测试：纯文件/纯函数，离线。"""
import json
import sqlite3
import time
from pathlib import Path

import pytest

import health


def write_site(root: Path, domain: str, counts: dict) -> None:
    d = root / domain
    d.mkdir(parents=True, exist_ok=True)
    for kind, n in counts.items():
        name = "urls.txt" if kind == "posts" else f"{kind}.urls.txt"
        (d / name).write_text("".join(f"https://{domain}/{i}\n" for i in range(n)),
                              encoding="utf-8")


@pytest.fixture(autouse=True)
def clean_caches(monkeypatch):
    monkeypatch.setattr(health, "_LAST", {"ts": 0.0, "total": None})
    monkeypatch.setattr(health, "_COUNT_CACHE", {})


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    write_site(root, "a.test", {"posts": 3, "pages": 1})
    write_site(root, "b.test", {"posts": 2, "media": 1})
    return root


def make_requests_db(path: Path, rows) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE requests(
        domain TEXT, kind TEXT, key TEXT, url TEXT, channel TEXT, label TEXT,
        state TEXT, retries INTEGER DEFAULT 0, error TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (domain, kind, key))""")
    conn.executemany(
        "INSERT INTO requests(domain,kind,key,url,state,error,created,updated) "
        "VALUES(?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


def test_snapshot_fields_and_totals(data):
    (data / "supervise_state.json").write_text(json.dumps({
        "running": [{"domain": "a.test", "kind": "posts"}],
        "pending": ["a.test:pages", "b.test:posts"],
        "completed": ["c.test:posts", "d.test:posts"],
        "stalled": ["e.test:posts"],
    }), encoding="utf-8")
    (data / "CRAWL_PAUSED").touch()

    snap = health.snapshot(data_dir=data)
    assert set(snap) == {"ts", "pid", "running", "pending", "stalled", "done",
                         "paused", "total_rows", "rate_5m", "errors_1h", "last_error"}
    assert snap["running"] == ["a.test:posts"]
    assert snap["pending"] == 2
    assert snap["stalled"] == 1
    assert snap["done"] == 2
    assert snap["paused"] is True
    assert snap["total_rows"] == 7          # 3+1 + 2+1
    assert isinstance(snap["pid"], int)
    assert isinstance(snap["ts"], str) and snap["ts"].startswith(str(time.localtime().tm_year))


def test_snapshot_explicit_overrides(data):
    (data / "CRAWL_PAUSED").touch()
    snap = health.snapshot(data_dir=data, running=["x:posts"], pending=0,
                           stalled=0, done=5, paused=False)
    assert snap["running"] == ["x:posts"]
    assert (snap["pending"], snap["stalled"], snap["done"]) == (0, 0, 5)
    assert snap["paused"] is False


def test_compute_rate():
    assert health.compute_rate(None, None, 100, 10) == 0.0
    assert health.compute_rate(100, 1000.0, 160, 1060.0) == 60.0     # +60 行/60 秒
    assert health.compute_rate(100, 1000.0, 160, 1400.0) == 0.0      # 超 5 分钟窗口
    assert health.compute_rate(100, 1000.0, 90, 1060.0) == 0.0       # 回退
    assert health.compute_rate(100, 1000.0, 100, 1000.0) == 0.0      # 时间为 0


def test_errors_1h_and_last_error(data):
    now = time.time()
    recent = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 60))
    old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 7200))
    db = make_requests_db(data / "state.db", [
        ("a.test", "posts", "k1", "u1", "error", "recent boom", recent, recent),
        ("a.test", "posts", "k2", "u2", "error", "old boom", old, old),
        ("b.test", "posts", "k3", "u3", "done", "", recent, recent),
    ])
    snap = health.snapshot(data_dir=data, db_path=db, running=[], pending=0,
                           stalled=0, done=0, paused=False)
    assert snap["errors_1h"] == 1
    assert snap["last_error"] == "recent boom"


def test_write_health_atomic_roundtrip(tmp_path, data):
    target = tmp_path / "health.json"
    payload = health.write_health(target, data_dir=data, running=["a.test:posts"],
                                  pending=1, stalled=0, done=0, paused=True)
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert not list(tmp_path.glob("*.tmp"))
    assert payload["running"] == ["a.test:posts"]


def test_missing_data_dir_and_db_do_not_crash(tmp_path):
    snap = health.snapshot(data_dir=tmp_path / "nope",
                           db_path=tmp_path / "nope.db", paused=False)
    assert snap["total_rows"] == 0
    assert snap["running"] == [] and snap["pending"] == 0
    assert snap["errors_1h"] == 0 and snap["last_error"] == ""


def test_total_rows_cache_invalidates(data):
    assert health.total_rows(data) == 7
    assert health.total_rows(data) == 7                  # 命中缓存
    with open(data / "a.test" / "urls.txt", "a", encoding="utf-8") as f:
        f.write("https://a.test/new\n")
    assert health.total_rows(data) == 8                  # mtime/size 变化后失效
