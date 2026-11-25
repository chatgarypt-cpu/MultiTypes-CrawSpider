"""Counters for one detail-channel invocation. A retry is another attempt."""
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class DetailCounts:
    discovered: int = 0
    attempted: int = 0
    request_failures: Counter[str] = field(default_factory=Counter)
    rejections: Counter[str] = field(default_factory=Counter)

    def report(self) -> dict:
        return {
            "version": 1,
            "discovered": self.discovered,
            "attempted": self.attempted,
            "request_failures": dict(self.request_failures),
            "rejections": dict(self.rejections),
        }


def site_quality(db_path: Path, now: datetime | None = None) -> dict:
    """Read seven days of completed invocations from the background collector."""
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    report = {"since": start.isoformat(), "until": end.isoformat(), "sites": []}
    path = Path(db_path)
    if not path.is_file():
        return report
    groups = {}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
                return report
            rows = conn.execute("SELECT domain, saved, status, note FROM runs WHERE finished >= ? AND finished <= ?",
                                (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")))
            for domain, saved, status, raw in rows:
                group = groups.setdefault(domain, {
                    "domain": domain, "runs": 0, "measured_runs": 0, "discovered": 0, "attempted": 0,
                    "incomplete_runs": 0, "saved": 0, "request_failed": 0, "rejected": 0, "reasons": Counter(),
                })
                group["runs"] += 1
                if status in {"partial", "interrupted", "blocked", "failed"}:
                    group["incomplete_runs"] += 1
                group["saved"] += saved or 0
                try:
                    note = json.loads(raw or "{}")
                except (ValueError, TypeError):
                    continue
                metrics = note.get("quality") if isinstance(note, dict) else None
                if not isinstance(metrics, dict) or metrics.get("version") != 1:
                    continue
                group["measured_runs"] += 1
                group["discovered"] += metrics["discovered"]
                group["attempted"] += metrics["attempted"]
                for source, field in (("request_failures", "request_failed"), ("rejections", "rejected")):
                    counts = metrics[source]
                    group[field] += sum(counts.values())
                    group["reasons"].update(counts)
    except sqlite3.Error:
        report["error"] = "运行记录暂时无法读取"
        return report
    for group in groups.values():
        attempted = group["attempted"]
        group["fetched"] = attempted - group["request_failed"]
        accepted = group["fetched"] - group["rejected"]
        group["acceptance"] = accepted / attempted if attempted else None
        group["top_reasons"] = group.pop("reasons").most_common(3)
    report["sites"] = sorted(groups.values(), key=lambda g: (-(g["request_failed"] + g["rejected"]), -g["incomplete_runs"], g["domain"]))
    return report
