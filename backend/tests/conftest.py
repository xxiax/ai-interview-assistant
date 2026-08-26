"""测试环境：固定安全配置，并为每个测试隔离 SQLite。"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

AUTH_TOKEN = "test-access-token-0123456789abcdef0123456789"
FERNET_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
FUNASR_TOKEN = "test-funasr-token"
os.environ.setdefault("AI_AUTH_TOKEN", AUTH_TOKEN)
os.environ.setdefault("AI_CONFIG_ENCRYPTION_KEY", FERNET_KEY)
os.environ.setdefault("AI_FUNASR_TOKEN", FUNASR_TOKEN)
os.environ.setdefault("AI_ASR_ENGINE", "llm")
os.environ.setdefault("AI_ALLOW_PRIVATE_LLM_HOSTS", "true")
os.environ.setdefault("AI_ALLOWED_ORIGINS", "")
os.environ.setdefault("AI_SKIP_MEDIA_PROBE_CHECK", "true")

from app import db
from app.main import app
from app.security import rate_limiter


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test-interview.db"
    monkeypatch.setenv("AI_DB_PATH", str(db_path))
    monkeypatch.setenv("AI_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("AI_CONFIG_ENCRYPTION_KEY", FERNET_KEY)
    monkeypatch.setenv("AI_FUNASR_TOKEN", FUNASR_TOKEN)
    monkeypatch.setenv("AI_ASR_ENGINE", "llm")
    monkeypatch.setenv("AI_ALLOW_PRIVATE_LLM_HOSTS", "true")
    monkeypatch.setenv("AI_ALLOWED_ORIGINS", "")
    monkeypatch.setenv("AI_SKIP_MEDIA_PROBE_CHECK", "true")
    rate_limiter.clear()
    conn = db.get_db()
    try:
        db.init_db(conn)
    finally:
        conn.close()
    yield
    rate_limiter.clear()


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers():
    return {"Authorization": f"Bearer {AUTH_TOKEN}"}
