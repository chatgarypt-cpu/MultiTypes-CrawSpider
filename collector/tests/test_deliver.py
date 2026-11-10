"""交付流水线（collector/deliver.py）测试：全部离线，只写 tmp_path。"""
import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

import deliver

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "collector" / "cli.py"


# ---------------------------------------------------------------- helpers
def write_site(root: Path, domain: str, counts: dict) -> Path:
    """造一个假站点：urls.txt / *.urls.txt + articles.jsonl / *.jsonl。"""
    d = root / domain
    d.mkdir(parents=True, exist_ok=True)
    for kind, n in counts.items():
        stem = "urls" if kind == "posts" else f"{kind}.urls"
        jsonl = "articles" if kind == "posts" else kind
        (d / f"{stem}.txt").write_text(
            "".join(f"https://{domain}/{kind}/{i}\n" for i in range(n)), encoding="utf-8")
        (d / f"{jsonl}.jsonl").write_text(
            "".join(json.dumps({"url": f"https://{domain}/{kind}/{i}", "title": f"t{i}"},
                               ensure_ascii=False) + "\n"
                    for i in range(n)), encoding="utf-8")
    return d


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_sources(path: Path, rows: list[tuple]) -> Path:
    """rows = [(domain, name, country, type, channel), ...]"""
    text = "".join(
        f"- domain: {d}\n  name: {name}\n  country: {country}\n"
        f"  type: {type_}\n  channel: {channel}\n"
        for d, name, country, type_, channel in rows)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_env, tmp_path):
    """2 个假站点 + 假 sources.yaml + coverage 快照；输出目录全部在 tmp_path。"""
    write_site(tmp_env, "alpha.test", {"posts": 3, "pages": 2, "media": 1})
    write_site(tmp_env, "beta.test", {"posts": 5})
    sources = write_sources(tmp_path / "sources.yaml", [
        ("alpha.test", "Alpha News", "Testland", "新闻", "wp-api"),
        ("beta.test", "Beta Org", "Testland", "智库", "sitemap"),
    ])
    coverage = {
        "report": [
            {"domain": "alpha.test", "channel": "wp-api",
             "got": {"posts": 3, "pages": 2, "media": 1},
             "expected": {"posts": 5, "pages": 2, "media": 4},
             "source": "X-WP-Total", "coverage": 0.25,
             "per_kind": {"posts": 0.6, "pages": 1.0, "media": 0.25}},
            {"domain": "beta.test", "channel": "sitemap",
             "got": {"posts": 5}, "expected": 8,
             "source": "sitemap <loc>", "coverage": 0.625},
        ],
        "verified": 2,
        "complete": 0,
    }
    (tmp_env / "coverage-20261107.json").write_text(
        json.dumps(coverage), encoding="utf-8")
    return {
        "data": tmp_env,
        "sources": str(sources),
        "csv_dir": tmp_path / "out" / "csv",
        "delivery_root": tmp_path / "out" / "delivery",
        "kwargs": {
            "data_dir": str(tmp_env),
            "csv_dir": str(tmp_path / "out" / "csv"),
            "delivery_root": str(tmp_path / "out" / "delivery"),
            "sources": str(sources),
        },
    }


def delivery_dir(env, date="20261107") -> Path:
    return Path(env["delivery_root"]) / date


# ---------------------------------------------------------------- tests
def test_deliverables_end_to_end(env):
    res = deliver.run(date="20261107", **env["kwargs"])
    dd = delivery_dir(env)

    assert sorted(p.name for p in dd.iterdir()) == sorted(
        ["site_manifest.csv", "residuals.csv", "交付说明.md", "coverage.json"])
    assert res["manifest_rows"] == 2
    assert res["dry_run"] is False

    manifest = read_csv(dd / "site_manifest.csv")
    assert [r["domain"] for r in manifest] == ["alpha.test", "beta.test"]
    assert (manifest[0]["posts"], manifest[0]["pages"], manifest[0]["media"]) == ("3", "2", "1")
    assert manifest[0]["total"] == "6"
    assert manifest[0]["name"] == "Alpha News"

    # 复用 export_csv：三种 kind 的 CSV 都导出了
    assert (env["csv_dir"] / "alpha.test.csv").exists()
    assert (env["csv_dir"] / "alpha.test.pages.csv").exists()
    assert (env["csv_dir"] / "alpha.test.media.csv").exists()
    with open(env["csv_dir"] / "alpha.test.csv", encoding="utf-8-sig", newline="") as f:
        assert len(list(csv.DictReader(f))) == 3

    coverage = json.loads((dd / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["verified"] == 2
    assert len(coverage["report"]) == 2

    readme = (dd / "交付说明.md").read_text(encoding="utf-8")
    for needle in ("南太平洋信源采集数据", "交付物清单", "site_manifest.csv",
                   "residuals.csv", "合计", "| posts | 8 |"):
        assert needle in readme
    # 合计 = posts 8 + pages 2 + media 1
    assert "**11**" in readme


def test_manifest_missing_and_status(env):
    deliver.run(date="20261107", **env["kwargs"])
    manifest = read_csv(delivery_dir(env) / "site_manifest.csv")
    alpha = manifest[0]
    assert alpha["status"] == "有残差"
    # missing = (5-3) + (2-2) + (4-1) = 5
    assert alpha["missing"] == "5"
    assert float(alpha["coverage"]) == 0.25
    assert alpha["files"] == ("alpha.test.csv,alpha.test.pages.csv,alpha.test.media.csv")

    beta = manifest[1]
    assert beta["missing"] == "3"       # 8 - 5
    assert beta["status"] == "有残差"
    assert beta["channel"] == "sitemap"


def test_manifest_includes_unconfigured_site_dirs(env):
    # 有数据但不在 sources.yaml 的目录要追加进清单并标注
    write_site(env["data"], "gamma.test", {"posts": 2})
    deliver.run(date="20261107", **env["kwargs"])
    manifest = read_csv(delivery_dir(env) / "site_manifest.csv")
    gamma = [r for r in manifest if r["domain"] == "gamma.test"]
    assert gamma and gamma[0]["name"] == ""
    assert "未在 sources.yaml 配置" in gamma[0]["note"]
    assert gamma[0]["status"] == "入口窗口"     # 无覆盖率记录


def test_residuals_quantified_and_unverifiable(env):
    write_site(env["data"], "delta.test", {"posts": 2})
    payload = {
        "report": [
            {"domain": "alpha.test", "channel": "wp-api",
             "expected": {"posts": 5, "pages": 2, "media": 4}},
            {"domain": "beta.test", "channel": "sitemap", "expected": 8},
            {"domain": "delta.test", "channel": "rss", "expected": None,
             "source": "unverifiable"},
        ]
    }
    rows = deliver.build_residuals(payload, env["data"])
    by_key = {(r["domain"], r["kind"]): r for r in rows}
    assert by_key[("alpha.test", "media")]["missing"] == 3
    assert by_key[("alpha.test", "posts")]["missing"] == 2
    assert "pages" not in {r["kind"] for r in rows if r["domain"] == "alpha.test"}
    assert by_key[("alpha.test", "media")]["ratio"] == 0.25
    assert "X-WP-Total 口径差额" in by_key[("alpha.test", "media")]["verdict"]
    assert "非 wp-api 通道" in by_key[("beta.test", "posts")]["verdict"]
    # 无法验证：expected/missing/ratio 留空，排在最后
    unver = by_key[("delta.test", "posts")]
    assert unver["expected"] == "" and unver["missing"] == ""
    assert "入口窗口" in unver["verdict"]
    assert rows[-1]["domain"] == "delta.test"
    # 残差按缺量从大到小
    quantified = [int(r["missing"]) for r in rows if r["missing"] != ""]
    assert quantified == sorted(quantified, reverse=True)


def test_residuals_excludes_complete_site(env):
    write_site(env["data"], "done.test", {"posts": 4})
    payload = {"report": [
        {"domain": "done.test", "channel": "wp-api", "expected": {"posts": 4}},
    ]}
    assert deliver.build_residuals(payload, env["data"]) == []


def test_no_coverage_warns_but_still_writes(env, capsys):
    (env["data"] / "coverage-20261107.json").unlink()
    deliver.run(date="20261107", **env["kwargs"])
    err = capsys.readouterr().err
    assert "coverage-*.json" in err and "verify" in err
    dd = delivery_dir(env)
    assert read_csv(dd / "residuals.csv") == []          # 只有表头
    coverage = json.loads((dd / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["report"] == [] and coverage["source"] is None
    manifest = read_csv(dd / "site_manifest.csv")
    assert manifest[0]["coverage"] == ""
    assert manifest[0]["status"] == "入口窗口"


def test_date_without_snapshot_falls_back_to_latest(env, capsys):
    old = env["data"] / "coverage-20261101.json"
    (env["data"] / "coverage-20261107.json").rename(old)
    res = deliver.run(date="20261107", **env["kwargs"])
    err = capsys.readouterr().err
    assert "最新快照" in err and "coverage-20261101.json" in err
    assert res["coverage_source"].endswith("coverage-20261101.json")


def test_paused_site_status(env):
    (env["data"] / "CRAWL_SKIP").write_text(
        "# user pause\nalpha.test:posts\n", encoding="utf-8")
    deliver.run(date="20261107", **env["kwargs"])
    manifest = read_csv(delivery_dir(env) / "site_manifest.csv")
    alpha = [r for r in manifest if r["domain"] == "alpha.test"][0]
    assert alpha["status"] == "暂停"
    assert "CRAWL_SKIP" in alpha["note"]


def test_dry_run_writes_nothing(env, capsys):
    res = deliver.run(date="20261107", dry_run=True, **env["kwargs"])
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert res["dry_run"] is True
    assert res["manifest_rows"] == 2
    assert not Path(env["delivery_root"]).exists()
    assert not Path(env["csv_dir"]).exists()


def test_no_export_skips_csv(env):
    deliver.run(date="20261107", no_export=True, **env["kwargs"])
    dd = delivery_dir(env)
    assert (dd / "site_manifest.csv").exists()
    assert not Path(env["csv_dir"]).exists()
    manifest = read_csv(dd / "site_manifest.csv")
    assert manifest[0]["files"] == ""


def test_date_validation():
    with pytest.raises(SystemExit):
        deliver.run(date="2026-11-07")


def test_cli_help_all_subcommands():
    for argv in (["--help"], ["status", "--help"], ["deliverables", "--help"],
                 ["verify", "--help"], ["dedupe", "--help"], ["reconcile", "--help"],
                 ["check-site", "--help"], ["pause", "--help"], ["resume", "--help"],
                 ["paused", "--help"], ["run", "--help"]):
        proc = subprocess.run([sys.executable, str(CLI), *argv],
                              capture_output=True, text=True, cwd=str(REPO))
        assert proc.returncode == 0, (argv, proc.stderr)
        assert "usage:" in proc.stdout
    proc = subprocess.run([sys.executable, str(CLI), "--help"],
                          capture_output=True, text=True, cwd=str(REPO))
    for command in ("status", "deliverables", "verify", "dedupe", "reconcile",
                    "check-site", "pause", "resume", "paused", "run"):
        assert command in proc.stdout
