import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_llm_configs():
    """清理 llm 类型配置，保证测试可重复运行且不污染其他测试（测试写真实 interview.db）。"""
    conn = db.get_db()
    try:
        conn.execute("DELETE FROM configs WHERE type = 'llm'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_db()
    try:
        conn.execute("DELETE FROM configs WHERE type = 'llm'")
        conn.commit()
    finally:
        conn.close()


def test_save_and_get_llm_config():
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


def test_activate_switches_active():
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
