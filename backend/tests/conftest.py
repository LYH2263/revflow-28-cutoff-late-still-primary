"""测试共用：固定 DATA_DIR（导入即定，运行中不再切换，避免旧连接指向失效库）。

每个测试模块在夹具里调用 reset_database() 重建一份空库并重新播种夹具。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# 全程唯一且不变的数据目录
TEST_DATA_DIR = ROOT / "backend" / "data_test"
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ.setdefault("FIXTURES_DIR", str(ROOT / "fixtures"))


def reset_database() -> None:
    """删库并经 init_db 重建播种；DATA_DIR 不变，所有连接始终指向同一文件。"""
    db_file = TEST_DATA_DIR / "app.db"
    if db_file.exists():
        db_file.unlink()
    from app import seed

    seed.init_db()
