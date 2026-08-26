from __future__ import annotations

import httpx2 as httpx
import pytest

from app import db, search


@pytest.mark.asyncio
async def test_no_search_config():
    assert await search.search_web("测试") == []


@pytest.mark.asyncio
async def test_search_failure_degrades_to_empty_results(monkeypatch):
    conn = db.get_db()
    try:
        db.save_config(
            conn,
            "search",
            "google",
            {"engine": "google", "api_key": "secret", "cx": "cx-id"},
            True,
        )
    finally:
        conn.close()

    async def broken(*_args, **_kwargs):
        request = httpx.Request("GET", "https://example.com")
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(search, "_search_google", broken)
    assert await search.search_web("测试") == []


@pytest.mark.asyncio
async def test_retired_bing_config_degrades_without_spending_budget(monkeypatch):
    """历史遗留的 bing 配置：运行时按不可用降级，且不再消耗搜索预算。"""
    conn = db.get_db()
    try:
        db.save_config(
            conn,
            "search",
            "bing",
            {"engine": "bing", "api_key": "secret", "cx": ""},
            True,
        )
    finally:
        conn.close()

    reserved = []

    async def fail_if_reserved(*_args):
        reserved.append(True)
        raise AssertionError("退役引擎不应再消耗搜索预算")

    monkeypatch.setattr(search.cost_control, "reserve_search_request", fail_if_reserved)
    assert await search.search_web("测试") == []
    assert reserved == []


def test_bing_config_is_rejected_at_save_time(client, auth_headers):
    response = client.post(
        "/api/configs/search",
        json={
            "name": "旧 Bing",
            "data": {"engine": "bing", "api_key": "secret", "cx": ""},
            "is_active": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "退役" in response.text


def test_search_results_are_shape_and_length_limited():
    items = [
        {"title": "T" * 500, "link": "L" * 3000, "snippet": "S" * 1500},
        "invalid",
    ]
    cleaned = search._clean_results(items, "title", "link", "snippet")
    assert len(cleaned) == 1
    assert len(cleaned[0]["title"]) == 300
    assert len(cleaned[0]["link"]) == 2000
    assert len(cleaned[0]["snippet"]) == 1000


@pytest.mark.asyncio
async def test_malformed_search_payload_degrades_to_empty_results(monkeypatch):
    conn = db.get_db()
    try:
        db.save_config(
            conn,
            "search",
            "google",
            {"engine": "google", "api_key": "secret", "cx": "cx-id"},
            True,
        )
    finally:
        conn.close()

    class Response:
        status_code = 200

        def json(self):
            return ["not-an-object"]

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(search.httpx, "AsyncClient", Client)
    assert await search.search_web("测试") == []
