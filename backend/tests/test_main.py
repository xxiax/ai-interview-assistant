from __future__ import annotations

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def test_health_is_anonymous_and_has_security_headers(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_readiness_reports_funasr_unavailable(client, monkeypatch):
    async def unavailable():
        raise RuntimeError("offline")

    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    monkeypatch.setattr("app.main.asr.check_funasr_ready", unavailable)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "FunASR 尚未就绪"


def test_openapi_declares_bearer_security(client, monkeypatch):
    # 文档默认关闭；显式打开后 OpenAPI 才可用且声明 Bearer 认证
    monkeypatch.setenv("AI_DOCS_ENABLED", "true")
    docs_client = TestClient(create_app())
    schema = docs_client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert schema["paths"]["/api/sessions"]["get"]["security"] == [{"HTTPBearer": []}]
    assert "security" not in schema["paths"]["/health"]["get"]


def test_docs_are_disabled_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_startup_rejects_missing_security_config(monkeypatch):
    monkeypatch.delenv("AI_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="AI_AUTH_TOKEN"), TestClient(create_app()):
        pass


def test_startup_rejects_missing_funasr_token(monkeypatch):
    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    monkeypatch.delenv("AI_FUNASR_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="AI_FUNASR_TOKEN"), TestClient(create_app()):
        pass


def test_startup_does_not_require_funasr_token_for_llm_engine(monkeypatch):
    monkeypatch.setenv("AI_ASR_ENGINE", "llm")
    monkeypatch.delenv("AI_FUNASR_TOKEN", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200


def test_startup_does_not_require_llm_host_allowlist(monkeypatch):
    """LLM 主机不再要求环境白名单；请求时仍执行 HTTPS 与 SSRF 校验。"""
    monkeypatch.setenv("AI_ALLOW_PRIVATE_LLM_HOSTS", "false")
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200


def test_explicit_env_file_is_loaded(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AI_AUTH_TOKEN=env-file-token-0123456789abcdef012345\n"
        "AI_CONFIG_ENCRYPTION_KEY=MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=\n",
        encoding="utf-8",
    )
    process_env = os.environ.copy()
    for key in [name for name in process_env if name.startswith("COV_CORE_")]:
        process_env.pop(key, None)
    process_env.pop("AI_AUTH_TOKEN", None)
    process_env.pop("AI_CONFIG_ENCRYPTION_KEY", None)
    process_env["AI_ENV_FILE"] = str(env_file)
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    process_env["PYTHONPATH"] = backend_dir
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, app.main; print(os.environ['AI_AUTH_TOKEN'])",
        ],
        cwd=tmp_path,
        env=process_env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert result.stdout.strip() == "env-file-token-0123456789abcdef012345"
