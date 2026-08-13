import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    """让 db.get_db() 指向临时数据库，避免污染真实 interview.db。"""
    db_path = tmp_path / "test_configs.db"
    original_get_db = db.get_db

    def _get_db(*args, **kwargs):
        conn = original_get_db(str(db_path))
        db.init_db(conn)
        return conn

    monkeypatch.setattr(db, "get_db", _get_db)


def test_save_and_get_llm_config(temp_db):
    resp = client.post("/api/configs/llm", json={
        "name": "我的中转",
        "data": {"base_url": "http://x", "api_key": "k", "model": "gpt-4o"},
        "is_active": True,
    })
    assert resp.status_code == 200
    config_id = resp.json()["id"]

    resp = client.get("/api/configs/llm")
    assert resp.status_code == 200
    configs = resp.json()
    assert len(configs) == 1
    assert configs[0]["name"] == "我的中转"
    assert configs[0]["is_active"] is True


def test_activate_switches_active(temp_db):
    client.post("/api/configs/llm", json={
        "name": "配置A", "data": {"base_url": "a"}, "is_active": True,
    })
    resp = client.post("/api/configs/llm", json={
        "name": "配置B", "data": {"base_url": "b"}, "is_active": True,
    })
    config_b_id = resp.json()["id"]

    configs = client.get("/api/configs/llm").json()
    active = [c for c in configs if c["is_active"]]
    assert len(active) == 1
    assert active[0]["id"] == config_b_id


def test_activate_endpoint(temp_db):
    """直接调用 activate 端点：启用一个配置，另一个保持非激活。"""
    resp_a = client.post("/api/configs/llm", json={
        "name": "配置A", "data": {"base_url": "a"}, "is_active": False,
    })
    resp_b = client.post("/api/configs/llm", json={
        "name": "配置B", "data": {"base_url": "b"}, "is_active": False,
    })
    config_a_id = resp_a.json()["id"]
    config_b_id = resp_b.json()["id"]

    # 激活配置 B
    resp = client.post(f"/api/configs/llm/activate/{config_b_id}")
    assert resp.status_code == 200
    active = resp.json()
    assert active["id"] == config_b_id
    assert active["is_active"] is True

    configs = client.get("/api/configs/llm").json()
    by_id = {c["id"]: c for c in configs}
    assert by_id[config_b_id]["is_active"] is True
    assert by_id[config_a_id]["is_active"] is False


def test_activate_endpoint_404(temp_db):
    """激活不存在的配置应返回 404。"""
    resp = client.post("/api/configs/llm/activate/9999")
    assert resp.status_code == 404
