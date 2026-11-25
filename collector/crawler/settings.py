"""运行期设置：robots 闸门与全局暂停（工作台「设置」的后端）。

优先级（高 → 低）：

1. `CRAWL_BYPASS` 环境变量 / `data/CRAWL_BYPASS` 哨兵文件 —— 一键放开（已有行为，运维用）；
2. `data/settings.json` 的 `respect_robots` —— 设置页写的持久配置；
3. 默认值 `respect_robots = True`。

全部判定都收在这里，预览、探测、正式采集、浏览器出口共用同一个结论，避免
"设置里关了 robots，试采仍被拦"这种两套开关不一致的问题。
"""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from crawler import config

DEFAULTS = {"respect_robots": True}
FILE = "settings.json"
PAUSE_FILE = "CRAWL_PAUSED"


def _directory() -> Path:
    # One-off workers keep their rows in a separate directory.
    return Path(os.environ.get("CRAWL_SETTINGS_DIR") or config.DATA)


def path() -> Path:
    return _directory() / FILE


def read() -> dict:
    """当前设置（含默认值）；文件坏了就只返回默认值，不抛。"""
    data = dict(DEFAULTS)
    try:
        stored = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return data
    if isinstance(stored, dict):
        data.update({key: value for key, value in stored.items() if key in DEFAULTS and isinstance(value, bool)})
    return data


def write(values=None) -> dict:
    """合并写入（只认白名单键），返回写入后的设置。"""
    data = read()
    for key, value in (values or {}).items():
        if key in DEFAULTS:
            data[key] = bool(value)
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as handle:
        staged = Path(handle.name)
        try:
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
    return data


def respect_robots() -> bool:
    """是否受 robots.txt 约束（False = 全站放行）。"""
    if config.bypass():
        return False
    return bool(read().get("respect_robots", True))


def allow_target(ignore=False):
    """给 public_get 的 allow_target：None = 不检查 robots。

    `ignore=True` 是"仅本次调用"的豁免（试采按钮），与全局设置叠加。
    """
    if ignore or not respect_robots():
        return None
    from crawler.politeness import PUBLIC_ROBOTS
    return PUBLIC_ROBOTS.allowed


def paused() -> bool:
    """全局暂停（正式采集入口会检查）。"""
    return (_directory() / PAUSE_FILE).is_file()


def set_paused(value) -> bool:
    """写/删暂停哨兵；返回写后的状态。"""
    target = _directory() / PAUSE_FILE
    if value:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("paused\n", encoding="utf-8")
    else:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
    return paused()


def status() -> dict:
    """给设置页的状态快照。"""
    return {"respect_robots": respect_robots(), "paused": paused(), "bypass": config.bypass()}
