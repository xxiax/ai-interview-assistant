from __future__ import annotations


def test_global_prompt_requires_auth(client):
    assert client.get("/api/settings/prompt").status_code == 401
    assert client.put("/api/settings/prompt", json={"prompt": "x"}).status_code == 401


def test_global_prompt_roundtrip_and_clear(client, auth_headers):
    assert client.get("/api/settings/prompt", headers=auth_headers).json() == {"prompt": ""}

    response = client.put(
        "/api/settings/prompt",
        json={"prompt": "  你是资深后端面试官  "},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"prompt": "你是资深后端面试官"}

    response = client.put(
        "/api/settings/prompt",
        json={"prompt": "   "},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"prompt": ""}


def test_global_prompt_rejects_oversized_value(client, auth_headers):
    response = client.put(
        "/api/settings/prompt",
        json={"prompt": "长" * 8001},
        headers=auth_headers,
    )
    assert response.status_code == 422
