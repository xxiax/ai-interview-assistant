"""pytest 全局配置：隔离测试与真实 interview.db。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """每个测试使用独立的临时数据库。

    通过 monkeypatch 把 db.get_db 指向 tmp_path 下的临时库并 init_db，
    避免污染 cwd 下的真实 interview.db（final review I1）。
    同时设置 AI_DB_PATH 环境变量，保证任何经由 db.get_db() 的连接都指向临时库。
    """
    db_path = tmp_path / "test-interview.db"
    monkeypatch.setenv("AI_DB_PATH", str(db_path))

    original_get_db = db.get_db

    def _get_db(*args, **kwargs):
        conn = original_get_db(*args, **kwargs)
        db.init_db(conn)
        return conn

    monkeypatch.setattr(db, "get_db", _get_db)
