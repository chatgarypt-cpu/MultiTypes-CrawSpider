"""Per-(domain, kind) crawl cursor: the single source of truth for "are we done?".

状态机（docs/REFACTOR_SPEC.md §2.2）::

    interrupted --(继续抓)--> interrupted / paused / partial / complete
    paused      --(继续抓)--> ...
    partial     --(确认不可得)--> 终态
    blocked     --(站点恢复)--> ...

"done" 只由 ``status == "complete"`` 决定：文件仍是 ``data/<domain>/cursor.json``
（kind != posts 时 ``<kind>.cursor.json``），保留 ``next_page/total_pages/saved/done``
等历史字段，JSON 可读、原子写入。

旧文件/旧调用方兼容：没有 ``status`` 时按 ``done`` 推导；``save_cursor(domain, cur,
kind)`` 仍可继续使用（collect.py 等旧 import 不受影响）。
"""
import json
import os
from datetime import datetime, timezone

from crawler import config

#: 合法状态集合（规格 2.2）
STATUS = frozenset({"complete", "partial", "blocked", "paused", "interrupted"})
STATUSES = ("complete", "partial", "blocked", "paused", "interrupted")

DEFAULT = {
    "next_page": 1,
    "total_pages": None,
    "saved": 0,
    "seen": 0,
    "done": False,
    "status": "interrupted",
    "reasons": {},
    "updated": "",
}
_MISSING = object()


def _paths(domain, kind="posts"):
    d = os.path.join(config.DATA, domain)      # 运行时读模块属性，便于测试 patch
    name = "cursor.json" if kind == "posts" else f"{kind}.cursor.json"
    return d, os.path.join(d, name)


def _normalise(cur):
    """补默认字段并强制 ``done == (status == "complete")``。"""
    had_done = "done" in cur
    for k, v in DEFAULT.items():
        cur.setdefault(k, dict(v) if isinstance(v, dict) else v)
    status, done = cur.get("status"), cur.get("done")
    if status not in STATUS:
        status = "complete" if done else "interrupted"
    elif had_done and bool(done) != (status == "complete"):
        # 旧工具（repair.py 等）会直接改 done 而不改 status：以 done 为准
        status = "complete" if done else "interrupted"
    cur["status"] = status
    cur["done"] = status == "complete"
    if not isinstance(cur.get("reasons"), dict):
        cur["reasons"] = {}
    try:
        cur["next_page"] = int(cur.get("next_page") or 1)
    except (TypeError, ValueError):
        cur["next_page"] = 1
    try:
        cur["saved"] = int(cur.get("saved") or 0)
    except (TypeError, ValueError):
        cur["saved"] = 0
    return cur


def load(domain, kind="posts"):
    """读取游标；文件缺失/损坏时返回可安全续跑的默认游标。"""
    _, path = _paths(domain, kind)
    cur = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cur = dict(data)
        except (OSError, ValueError):
            pass
    return _normalise(cur)


def save(domain, kind="posts", **fields):
    """合并字段后原子写盘，返回写盘后的游标。

    ``done`` 与 ``status`` 只能二选一：传了 ``done`` 就按它推导 status，
    传了 ``status`` 就忽略 ``done``（保持 done == status=="complete"）。
    """
    fields = dict(fields)
    d, path = _paths(domain, kind)
    cur = load(domain, kind)
    legacy_done = fields.pop("done", _MISSING)
    status = fields.get("status")
    if status is not None and status not in STATUS:
        status = "complete" if legacy_done is True else "interrupted"
        fields["status"] = status
    if status is None and legacy_done is not _MISSING:
        fields["status"] = "complete" if legacy_done else "interrupted"
    cur.update(fields)
    if "status" in fields:                    # 显式状态优先，done 跟着推导
        cur["done"] = fields["status"] == "complete"
    _normalise(cur)
    cur["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False)
    os.replace(tmp, path)                     # 原子替换：不会留下半截游标
    return cur


def _existing_reasons(domain, kind, extra):
    """mark_* 没显式给 reasons 时，在磁盘已有计数上累加。"""
    given = extra.pop("reasons", None)
    return dict(given if given is not None else load(domain, kind).get("reasons") or {})


def _bump(reasons, reason):
    if not reason:
        return reasons
    if isinstance(reason, dict):
        for k, v in reason.items():
            reasons[str(k)] = reasons.get(str(k), 0) + int(v)
    else:
        reasons[str(reason)] = reasons.get(str(reason), 0) + 1
    return reasons


def mark_complete(domain, kind="posts", **extra):
    """确认抓完了：翻过 total_pages（或 400 invalid page）且没有缺页。"""
    reasons = extra.pop("reasons", None)
    if reasons is not None:
        extra["reasons"] = dict(reasons)
    return save(domain, kind, status="complete", **extra)


def mark_partial(domain, kind="posts", reason="", **extra):
    """有缺口但已确认不可得（如持久空页被跳过）。"""
    reasons = _existing_reasons(domain, kind, extra)
    _bump(reasons, reason)
    extra["reasons"] = reasons
    if reason:
        extra.setdefault("last_reason", reason)
    return save(domain, kind, status="partial", **extra)


def mark_blocked(domain, kind="posts", reason="", **extra):
    """403/登录墙/站点不可用：不要再假装完成。"""
    reasons = _existing_reasons(domain, kind, extra)
    _bump(reasons, reason)
    extra["reasons"] = reasons
    extra["blocked_reason"] = reason
    if reason:
        extra.setdefault("last_reason", reason)
    return save(domain, kind, status="blocked", **extra)


def mark_paused(domain, kind="posts", reason="limit", **extra):
    """主动暂停（例如 --limit 到了），可随时续跑。"""
    reasons = _existing_reasons(domain, kind, extra)
    _bump(reasons, reason)
    extra["reasons"] = reasons
    extra.setdefault("last_reason", reason)
    return save(domain, kind, status="paused", **extra)


def mark_interrupted(domain, kind="posts", reason="", **extra):
    """运行中断/瞬时失败，游标保留原地，下次继续。"""
    reasons = _existing_reasons(domain, kind, extra)
    _bump(reasons, reason)
    extra["reasons"] = reasons
    if reason:
        extra.setdefault("last_reason", reason)
    return save(domain, kind, status="interrupted", **extra)


def is_complete(domain, kind="posts"):
    return load(domain, kind)["status"] == "complete"


# ---------- 旧接口（collect.py / 其他 channel 仍在用） ----------
def load_cursor(domain, kind="posts"):
    return load(domain, kind)


def save_cursor(domain, cur, kind="posts"):
    """兼容 store.save_cursor(domain, cur, kind) 的旧签名。"""
    return save(domain, kind, **dict(cur or {}))
