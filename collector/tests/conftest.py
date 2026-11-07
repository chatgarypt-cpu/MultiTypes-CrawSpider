"""测试公共夹具：把 DATA/DB 指到临时目录，保证测试离线、不碰真实数据。"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]          # 仓库根
COLLECTOR = ROOT / "collector"
sys.path.insert(0, str(COLLECTOR))

PATCH_DATA = [
    "crawler.config", "crawler.store", "crawler.request", "crawler.runner",
    "crawler.status", "crawler.web", "crawler.discover", "crawler.export_csv",
]


@pytest.fixture
def tmp_env(monkeypatch, tmp_path):
    """数据目录/状态库隔离到 tmp_path。返回临时 data 目录。"""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    for name in PATCH_DATA:
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        if hasattr(mod, "DATA"):
            monkeypatch.setattr(mod, "DATA", str(data))
        if hasattr(mod, "DB"):
            monkeypatch.setattr(mod, "DB", str(data / "state.db"))
    # export_csv 用的是模块内常量路径
    try:
        import export_csv as exp
        monkeypatch.setattr(exp, "DATA", str(data), raising=False)
        monkeypatch.setattr(exp, "CSV_DIR", str(data / "csv"), raising=False)
    except Exception:
        pass
    return data


@pytest.fixture
def site_dir(tmp_env):
    """建一个站点数据目录，返回 (domain, dir)。"""
    def _make(domain="example.test"):
        d = tmp_env / domain
        d.mkdir(parents=True, exist_ok=True)
        return domain, d
    return _make
