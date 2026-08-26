from __future__ import annotations

import pytest

from app import db

LLM_DATA = {
    "base_url": "https://llm.example/v1",
    "api_key": "llm-secret-value",
    "model": "test-model",
    "auth_field": "Authorization",
}


def test_config_requires_auth_and_rejects_unknown_type(client, auth_headers):
    assert client.get("/api/configs/llm").status_code == 401
    assert client.get("/api/configs/unknown", headers=auth_headers).status_code == 422


def test_secret_is_encrypted_and_never_returned(client, auth_headers):
    response = client.post(
        "/api/configs/llm",
        json={"name": "主配置", "data": LLM_DATA, "is_active": True},
        headers=auth_headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["secret_configured"] is True
    assert body["data"]["reasoning_effort"] == "low"
    assert "api_key" not in body["data"]
    assert "llm-secret-value" not in response.text

    listed = client.get("/api/configs/llm", headers=auth_headers)
    assert "llm-secret-value" not in listed.text
    assert "api_key" not in listed.json()[0]["data"]

    conn = db.get_db()
    try:
        raw_public = conn.execute("SELECT data FROM configs").fetchone()[0]
        ciphertext = conn.execute("SELECT ciphertext FROM config_secrets").fetchone()[0]
        active = db.get_active_config(conn, "llm")
    finally:
        conn.close()
    assert "llm-secret-value" not in raw_public
    assert "llm-secret-value" not in ciphertext
    assert active["data"]["api_key"] == "llm-secret-value"
    assert active["data"]["reasoning_effort"] == "low"


def test_llm_reasoning_effort_accepts_supported_values_and_rejects_unknown(
    client, auth_headers
):
    for effort in ("low", "medium", "high"):
        response = client.post(
            "/api/configs/llm",
            json={
                "name": f"思考强度-{effort}",
                "data": {
                    **LLM_DATA,
                    "api_key": f"secret-{effort}",
                    "reasoning_effort": effort,
                },
            },
            headers=auth_headers,
        )
        assert response.status_code == 201
        assert response.json()["data"]["reasoning_effort"] == effort

    invalid = client.post(
        "/api/configs/llm",
        json={
            "name": "非法思考强度",
            "data": {**LLM_DATA, "reasoning_effort": "ultra"},
        },
        headers=auth_headers,
    )
    assert invalid.status_code == 422


def test_config_schema_matches_path_and_validates_url(client, auth_headers):
    mismatch = client.post(
        "/api/configs/llm",
        json={"name": "错配", "data": {"engine": "bing", "api_key": "x"}},
        headers=auth_headers,
    )
    assert mismatch.status_code == 422

    insecure = client.post(
        "/api/configs/llm",
        json={
            "name": "不安全",
            "data": {
                **LLM_DATA,
                "api_key": "validation-secret",
                "base_url": "http://127.0.0.1:8000",
            },
        },
        headers=auth_headers,
    )
    assert insecure.status_code == 422
    assert "validation-secret" not in insecure.text

    missing_cx = client.post(
        "/api/configs/search",
        json={"name": "Google", "data": {"engine": "google", "api_key": "x"}},
        headers=auth_headers,
    )
    assert missing_cx.status_code == 422


def test_validation_errors_never_echo_secret_input(client, auth_headers):
    secret = "s" * 4097
    response = client.post(
        "/api/configs/llm",
        json={"name": "过长密钥", "data": {**LLM_DATA, "api_key": secret}},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert "***" in response.text


def test_activate_switches_only_same_type(client, auth_headers):
    first = client.post(
        "/api/configs/llm",
        json={"name": "A", "data": LLM_DATA, "is_active": True},
        headers=auth_headers,
    ).json()
    second = client.post(
        "/api/configs/llm",
        json={"name": "B", "data": {**LLM_DATA, "api_key": "second"}},
        headers=auth_headers,
    ).json()
    activated = client.post(
        f"/api/configs/llm/activate/{second['id']}",
        headers=auth_headers,
    )
    assert activated.status_code == 200
    configs = client.get("/api/configs/llm", headers=auth_headers).json()
    active_ids = [item["id"] for item in configs if item["is_active"]]
    assert active_ids == [second["id"]]
    assert first["id"] != second["id"]
    assert (
        client.post("/api/configs/llm/activate/9999", headers=auth_headers).status_code
        == 404
    )


def test_activate_rejects_oversized_sqlite_id(client, auth_headers):
    response = client.post(
        "/api/configs/llm/activate/9223372036854775808",
        headers=auth_headers,
    )
    assert response.status_code == 422


ASR_DATA = {"api_key": "asr-secret-value", "model": "whisper-large-v3"}


def test_asr_config_roundtrip_secret_encrypted(client, auth_headers):
    response = client.post(
        "/api/configs/asr",
        json={"name": "Groq 主账号", "data": ASR_DATA, "is_active": True},
        headers=auth_headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["type"] == "asr"
    assert body["secret_configured"] is True
    assert "api_key" not in body["data"]
    assert "asr-secret-value" not in response.text

    conn = db.get_db()
    try:
        active = db.get_active_config(conn, "asr")
    finally:
        conn.close()
    assert active["data"]["api_key"] == "asr-secret-value"


def test_asr_config_rejects_missing_key(client, auth_headers):
    response = client.post(
        "/api/configs/asr",
        json={"name": "缺 key", "data": {"model": "whisper-large-v3"}, "is_active": True},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_delete_config_removes_secret_and_entry(client, auth_headers):
    created = client.post(
        "/api/configs/asr",
        json={"name": "待删除", "data": ASR_DATA, "is_active": True},
        headers=auth_headers,
    ).json()
    response = client.delete(f"/api/configs/asr/{created['id']}", headers=auth_headers)
    assert response.status_code == 204
    assert client.get("/api/configs/asr", headers=auth_headers).json() == []

    conn = db.get_db()
    try:
        secrets = conn.execute(
            "SELECT COUNT(*) FROM config_secrets WHERE config_id = ?", (created["id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert secrets == 0


def test_delete_config_wrong_type_is_404(client, auth_headers):
    created = client.post(
        "/api/configs/llm",
        json={"name": "LLM", "data": LLM_DATA, "is_active": True},
        headers=auth_headers,
    ).json()
    assert (
        client.delete(f"/api/configs/search/{created['id']}", headers=auth_headers).status_code
        == 404
    )


def test_delete_missing_config_is_404(client, auth_headers):
    assert client.delete("/api/configs/llm/9999", headers=auth_headers).status_code == 404


# ---------- LLM /models 代理端点（客户端"获取模型"按钮） ----------


class _ModelsResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _patch_models_client(monkeypatch, response, capture=None):
    from app import routes_configs

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            if capture is not None:
                capture["url"] = url
                capture.update(kwargs)
            return response

    monkeypatch.setattr(routes_configs.httpx, "AsyncClient", Client)


def test_llm_models_probe_returns_sorted_models(client, auth_headers, monkeypatch):
    capture = {}
    _patch_models_client(
        monkeypatch,
        _ModelsResponse(
            payload={"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": "b-model"}]}
        ),
        capture,
    )
    response = client.post(
        "/api/configs/llm/models",
        json={"base_url": "https://llm.example/v1", "api_key": "probe-key"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"models": ["b-model", "gpt-4o", "gpt-4o-mini"]}
    assert capture["url"] == "https://llm.example/v1/models"
    assert capture["headers"]["Authorization"] == "Bearer probe-key"
    assert "probe-key" not in response.text


def test_llm_models_probe_uses_requested_auth_field(client, auth_headers, monkeypatch):
    capture = {}
    _patch_models_client(
        monkeypatch,
        _ModelsResponse(payload={"data": [{"id": "m"}]}),
        capture,
    )
    response = client.post(
        "/api/configs/llm/models",
        json={
            "base_url": "https://llm.example/v1",
            "api_key": "probe-key",
            "auth_field": "X-API-Key",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert capture["headers"] == {
        "X-API-Key": "probe-key",
        "Host": "llm.example",
    }


def test_llm_models_probe_rejects_invalid_key_with_401(client, auth_headers, monkeypatch):
    _patch_models_client(monkeypatch, _ModelsResponse(status_code=401))
    response = client.post(
        "/api/configs/llm/models",
        json={"base_url": "https://llm.example/v1", "api_key": "bad-key"},
        headers=auth_headers,
    )
    assert response.status_code == 401
    assert "无效或无权限" in response.json()["detail"]
    assert "bad-key" not in response.text


def test_llm_models_probe_requires_key_when_missing(client, auth_headers, monkeypatch):
    """空 key 且无激活 LLM 配置 → 400，并且不应发起出网请求。"""
    from app import routes_configs

    def _fail(*_args, **_kwargs):
        raise AssertionError("无 Key 时不应调用第三方")

    monkeypatch.setattr(routes_configs.httpx, "AsyncClient", _fail)
    response = client.post(
        "/api/configs/llm/models",
        json={"base_url": "https://llm.example/v1", "api_key": ""},
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert "缺少 API Key" in response.json()["detail"]


def test_llm_models_probe_reuses_saved_key_from_active_config(
    client, auth_headers, monkeypatch
):
    client.post(
        "/api/configs/llm",
        json={"name": "主配置", "data": LLM_DATA, "is_active": True},
        headers=auth_headers,
    )
    capture = {}
    _patch_models_client(
        monkeypatch, _ModelsResponse(payload={"data": [{"id": "m"}]}), capture
    )
    response = client.post(
        "/api/configs/llm/models",
        json={"base_url": "https://llm.example/v1", "api_key": ""},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"models": ["m"]}
    assert capture["headers"]["Authorization"] == "Bearer llm-secret-value"


def test_llm_models_probe_does_not_reuse_key_for_different_base_url(
    client, auth_headers, monkeypatch
):
    client.post(
        "/api/configs/llm",
        json={"name": "主配置", "data": LLM_DATA, "is_active": True},
        headers=auth_headers,
    )

    def _fail(*_args, **_kwargs):
        raise AssertionError("不同 Base URL 不应复用已保存 Key")

    from app import routes_configs

    monkeypatch.setattr(routes_configs.httpx, "AsyncClient", _fail)
    response = client.post(
        "/api/configs/llm/models",
        json={"base_url": "https://attacker.example/v1", "api_key": ""},
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert "显式填写" in response.json()["detail"]


def test_llm_models_probe_rejects_insecure_base_url(client, auth_headers):
    response = client.post(
        "/api/configs/llm/models",
        json={"base_url": "http://127.0.0.1:8000", "api_key": "k"},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_llm_models_probe_non_200_is_502(client, auth_headers, monkeypatch):
    _patch_models_client(monkeypatch, _ModelsResponse(status_code=500))
    response = client.post(
        "/api/configs/llm/models",
        json={"base_url": "https://llm.example/v1", "api_key": "k"},
        headers=auth_headers,
    )
    assert response.status_code == 502
    assert "模型列表获取失败" in response.json()["detail"]


def test_llm_config_rejects_removed_audio_model(client, auth_headers):
    response = client.post(
        "/api/configs/llm",
        json={
            "name": "多模态",
            "data": {**LLM_DATA, "audio_model": "gpt-4o-audio-preview"},
            "is_active": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_llm_models_probe_requires_auth(client):
    assert (
        client.post(
            "/api/configs/llm/models",
            json={"base_url": "https://llm.example/v1", "api_key": "k"},
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://127.0.0.1:7897",
        "https://proxy.example.com:8443",
        "socks5://127.0.0.1:1080",
        "socks5h://localhost:9050",
    ],
)
def test_network_config_accepts_four_proxy_schemes(client, auth_headers, proxy_url):
    response = client.post(
        "/api/configs/network",
        json={
            "name": "代理",
            "data": {"proxy_url": proxy_url, "api_key": "placeholder"},
            "is_active": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert response.json()["type"] == "network"


def test_network_config_rejects_bad_scheme(client, auth_headers):
    for bad in ("ftp://x", "socks4://x", "not-a-url", "127.0.0.1:7897"):
        response = client.post(
            "/api/configs/network",
            json={"name": "坏", "data": {"proxy_url": bad, "api_key": "p"}, "is_active": True},
            headers=auth_headers,
        )
        assert response.status_code == 422, bad


# ---------- 编辑模式(config_id 更新路径) ----------


def _save_llm_config(client, auth_headers, name="编辑测试"):
    response = client.post(
        "/api/configs/llm",
        json={
            "name": name,
            "data": {
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-original",
                "model": "deepseek-chat",
            },
            "is_active": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    return response.json()


def test_update_config_changes_fields_without_duplicate(client, auth_headers):
    created = _save_llm_config(client, auth_headers)
    config_id = created["id"]
    response = client.post(
        "/api/configs/llm",
        json={
            "name": "编辑后",
            "data": {
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-new",
                "model": "deepseek-reasoner",
            },
            "is_active": True,
            "config_id": config_id,
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == config_id
    assert body["name"] == "编辑后"
    listing = client.get("/api/configs/llm", headers=auth_headers).json()
    assert len([c for c in listing if c["id"] == config_id]) == 1
    assert len(listing) >= 1
    # 更新后仅一条该 id,且已删除的音频模型字段不会出现
    updated = next(c for c in listing if c["id"] == config_id)
    assert "audio_model" not in updated["data"]


def test_update_config_empty_key_keeps_stored_secret(client, auth_headers):
    created = _save_llm_config(client, auth_headers)
    config_id = created["id"]
    response = client.post(
        "/api/configs/llm",
        json={
            "name": "沿用密钥",
            "data": {
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "",
                "model": "deepseek-chat",
            },
            "is_active": True,
            "config_id": config_id,
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert response.json()["secret_configured"] is True


def test_update_config_unknown_id_returns_404(client, auth_headers):
    response = client.post(
        "/api/configs/llm",
        json={
            "name": "幽灵",
            "data": {
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-x",
                "model": "deepseek-chat",
            },
            "is_active": False,
            "config_id": 987654,
        },
        headers=auth_headers,
    )
    assert response.status_code == 404


def test_update_config_activating_deactivates_others(client, auth_headers):
    first = _save_llm_config(client, auth_headers, name="A")
    second = _save_llm_config(client, auth_headers, name="B")
    assert second["is_active"] is True
    response = client.post(
        "/api/configs/llm",
        json={
            "name": "A",
            "data": {
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-a",
                "model": "deepseek-chat",
            },
            "is_active": True,
            "config_id": first["id"],
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    listing = {c["id"]: c for c in client.get("/api/configs/llm", headers=auth_headers).json()}
    assert listing[first["id"]]["is_active"] is True
    assert listing[second["id"]]["is_active"] is False
