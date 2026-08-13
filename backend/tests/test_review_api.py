# backend/tests/test_review_api.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_review_without_llm_config():
    """未配置 LLM 时应返回明确错误。"""
    session_id = client.post("/api/sessions", json={"title": "测试"}).json()["id"]
    resp = client.post(f"/api/sessions/{session_id}/review")
    assert resp.status_code == 500
    assert "LLM" in resp.json()["detail"]
