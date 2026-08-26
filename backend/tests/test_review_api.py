from __future__ import annotations

from app import cost_control, db, llm, routes_review, search


def _ended_session_with_content(client, auth_headers):
    session = client.post(
        "/api/sessions",
        json={"title": "待复盘"},
        headers=auth_headers,
    ).json()
    client.post(
        f"/api/sessions/{session['id']}/start",
        json={"radio_mode": "pc"},
        headers=auth_headers,
    )
    conn = db.get_db()
    try:
        db.add_transcript(conn, session["id"], "pc", "什么是 FastAPI？")
        db.add_answer(
            conn, session["id"], "什么是 FastAPI？", "一个 Python Web 框架", "llm"
        )
    finally:
        conn.close()
    client.post(f"/api/sessions/{session['id']}/end", headers=auth_headers)
    return session["id"]


def test_review_requires_ended_session(client, auth_headers):
    session = client.post(
        "/api/sessions", json={"title": "未结束"}, headers=auth_headers
    ).json()
    response = client.post(
        f"/api/sessions/{session['id']}/review", headers=auth_headers
    )
    assert response.status_code == 409


def test_review_is_persisted_and_source_is_accurate(client, auth_headers, monkeypatch):
    session_id = _ended_session_with_content(client, auth_headers)

    async def fake_search(_query):
        return [{"title": "资料", "link": "https://example.com", "snippet": "事实"}]

    async def fake_review(transcripts, answers, search_results=None):
        assert transcripts and answers and search_results
        return "复盘正文"

    monkeypatch.setattr(search, "search_web", fake_search)
    monkeypatch.setattr(llm, "generate_review", fake_review)
    response = client.post(
        f"/api/sessions/{session_id}/review",
        json={"use_search": True},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["source"] == "search+llm"
    assert response.json()["content"] == "复盘正文"

    saved = client.get(f"/api/sessions/{session_id}/reviews", headers=auth_headers)
    assert saved.status_code == 200
    assert saved.json()[0]["id"] == response.json()["id"]


def test_review_is_idempotent_for_same_ended_session(client, auth_headers, monkeypatch):
    session_id = _ended_session_with_content(client, auth_headers)
    calls = 0

    async def fake_review(_transcripts, _answers, search_results=None):
        nonlocal calls
        calls += 1
        assert search_results is None
        return "只生成一次"

    monkeypatch.setattr(llm, "generate_review", fake_review)
    first = client.post(f"/api/sessions/{session_id}/review", headers=auth_headers)
    second = client.post(f"/api/sessions/{session_id}/review", headers=auth_headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert calls == 1
    assert routes_review._review_locks == {}


def test_search_failure_degrades_to_plain_llm(client, auth_headers, monkeypatch):
    session_id = _ended_session_with_content(client, auth_headers)

    async def no_results(_query):
        return []

    async def fake_review(_transcripts, _answers, search_results=None):
        assert search_results is None
        return "纯 LLM 复盘"

    monkeypatch.setattr(search, "search_web", no_results)
    monkeypatch.setattr(llm, "generate_review", fake_review)
    response = client.post(f"/api/sessions/{session_id}/review", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["source"] == "llm"


def test_review_search_is_opt_in(client, auth_headers, monkeypatch):
    session_id = _ended_session_with_content(client, auth_headers)

    async def unexpected_search(_query):
        raise AssertionError("未显式启用时不应把面试内容发送给搜索服务")

    async def fake_review(_transcripts, _answers, search_results=None):
        assert search_results is None
        return "默认隐私模式复盘"

    monkeypatch.setattr(search, "search_web", unexpected_search)
    monkeypatch.setattr(llm, "generate_review", fake_review)
    response = client.post(f"/api/sessions/{session_id}/review", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["source"] == "llm"


def test_review_errors_do_not_leak_upstream_details(client, auth_headers, monkeypatch):
    session_id = _ended_session_with_content(client, auth_headers)

    async def broken_review(*_args, **_kwargs):
        raise RuntimeError("upstream-secret-detail")

    monkeypatch.setattr(llm, "generate_review", broken_review)
    response = client.post(
        f"/api/sessions/{session_id}/review",
        json={"use_search": False},
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert "upstream-secret-detail" not in response.text


def test_review_input_limit_returns_413(client, auth_headers, monkeypatch):
    session_id = _ended_session_with_content(client, auth_headers)

    async def oversized_review(*_args, **_kwargs):
        raise llm.LLMInputTooLongError("复盘输入过长")

    monkeypatch.setattr(llm, "generate_review", oversized_review)
    response = client.post(f"/api/sessions/{session_id}/review", headers=auth_headers)
    assert response.status_code == 413
    assert response.json()["detail"] == "复盘输入过长"


def test_review_paid_usage_limits_return_retryable_429(
    client, auth_headers, monkeypatch
):
    session_id = _ended_session_with_content(client, auth_headers)

    async def limited_review(*_args, **_kwargs):
        raise db.UsageLimitExceeded("llm_tokens", 17)

    monkeypatch.setattr(llm, "generate_review", limited_review)
    response = client.post(f"/api/sessions/{session_id}/review", headers=auth_headers)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "17"

    async def busy_review(*_args, **_kwargs):
        raise cost_control.PaidCallBusyError("llm")

    monkeypatch.setattr(llm, "generate_review", busy_review)
    response = client.post(f"/api/sessions/{session_id}/review", headers=auth_headers)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"


def test_review_search_budget_limit_does_not_fall_back_to_paid_llm(
    client, auth_headers, monkeypatch
):
    session_id = _ended_session_with_content(client, auth_headers)

    async def limited_search(_query):
        raise db.UsageLimitExceeded("search_requests", 9)

    async def unexpected_review(*_args, **_kwargs):
        raise AssertionError("搜索预算耗尽时不应继续调用 LLM")

    monkeypatch.setattr(search, "search_web", limited_search)
    monkeypatch.setattr(llm, "generate_review", unexpected_review)
    response = client.post(
        f"/api/sessions/{session_id}/review",
        json={"use_search": True},
        headers=auth_headers,
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "9"
