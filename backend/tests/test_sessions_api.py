# backend/tests/test_sessions_api.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_create_session():
    resp = client.post("/api/sessions", json={"title": "测试面试"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "测试面试"
    assert data["status"] == "idle"
    return data["id"]


def test_get_session():
    session_id = test_create_session()
    resp = client.get(f"/api/sessions/{session_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == session_id


def test_end_session():
    session_id = test_create_session()
    resp = client.post(f"/api/sessions/{session_id}/end")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ended"


def test_get_transcripts_empty():
    session_id = test_create_session()
    resp = client.get(f"/api/sessions/{session_id}/transcripts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_sessions():
    test_create_session()
    test_create_session()
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert len(resp.json()) >= 2
