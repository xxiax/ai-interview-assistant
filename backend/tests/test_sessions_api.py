from __future__ import annotations

import pytest

from app import db


def _create(client, auth_headers, title="测试面试"):
    response = client.post("/api/sessions", json={"title": title}, headers=auth_headers)
    assert response.status_code == 201
    return response.json()


def test_rest_requires_bearer_auth(client):
    response = client.get("/api/sessions")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_session_state_machine_is_idle_recording_ended(client, auth_headers):
    session = _create(client, auth_headers)
    assert session["status"] == "idle"
    assert session["radio_mode"] == "pc"

    started = client.post(
        f"/api/sessions/{session['id']}/start",
        json={"radio_mode": "both"},
        headers=auth_headers,
    )
    assert started.status_code == 200
    assert started.json()["status"] == "recording"
    assert started.json()["radio_mode"] == "both"

    ended = client.post(f"/api/sessions/{session['id']}/end", headers=auth_headers)
    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"
    assert ended.json()["radio_mode"] == "both"
    assert ended.json()["ended_at"] is not None

    idempotent = client.post(f"/api/sessions/{session['id']}/end", headers=auth_headers)
    assert idempotent.status_code == 200
    assert idempotent.json()["ended_at"] == ended.json()["ended_at"]


def test_cannot_end_idle_session(client, auth_headers):
    session = _create(client, auth_headers)
    response = client.post(f"/api/sessions/{session['id']}/end", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["detail"]["current_status"] == "idle"


def test_ended_session_rejects_new_writes(client, auth_headers):
    session = _create(client, auth_headers)
    client.post(
        f"/api/sessions/{session['id']}/start",
        json={"radio_mode": "pc"},
        headers=auth_headers,
    )
    client.post(f"/api/sessions/{session['id']}/end", headers=auth_headers)

    conn = db.get_db()
    try:
        try:
            db.add_transcript(conn, session["id"], "pc", "结束后不应写入")
        except db.SessionStateError as exc:
            assert exc.current_status == "ended"
        else:
            raise AssertionError("ended 会话错误地接受了转写")
    finally:
        conn.close()


def test_input_validation_and_missing_children(client, auth_headers):
    assert (
        client.post(
            "/api/sessions", json={"title": "   "}, headers=auth_headers
        ).status_code
        == 422
    )
    session = _create(client, auth_headers)
    invalid_mode = client.post(
        f"/api/sessions/{session['id']}/start",
        json={"radio_mode": "invalid"},
        headers=auth_headers,
    )
    assert invalid_mode.status_code == 422
    assert (
        client.get(
            "/api/sessions/missing/transcripts", headers=auth_headers
        ).status_code
        == 404
    )
    assert (
        client.get("/api/sessions/missing/answers", headers=auth_headers).status_code
        == 404
    )
    assert (
        client.get("/api/sessions/missing/events", headers=auth_headers).status_code
        == 404
    )
    assert (
        client.get(
            "/api/sessions/missing/audio-chunks", headers=auth_headers
        ).status_code
        == 404
    )


def test_audio_chunk_statuses_can_be_pulled_after_ack(client, auth_headers):
    session = _create(client, auth_headers)
    client.post(
        f"/api/sessions/{session['id']}/start",
        json={"radio_mode": "pc"},
        headers=auth_headers,
    )
    conn = db.get_db()
    try:
        db.reserve_audio_chunk(
            conn,
            chunk_id="76aa92c8-28cc-4fd7-a818-54ec457305f5",
            session_id=session["id"],
            source="pc",
            codec="webm_opus",
            chunk_seq=0,
            captured_at="2026-08-17T00:00:00+00:00",
            duration_ms=1000,
            content_sha256="0" * 64,
        )
        db.mark_audio_chunk_status(
            conn,
            "76aa92c8-28cc-4fd7-a818-54ec457305f5",
            "failed",
            error_code="service_restart",
        )
    finally:
        conn.close()

    response = client.get(
        f"/api/sessions/{session['id']}/audio-chunks", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json() == [
        {
            "chunk_id": "76aa92c8-28cc-4fd7-a818-54ec457305f5",
            "session_id": session["id"],
            "source": "pc",
            "codec": "webm_opus",
            "chunk_seq": 0,
            "captured_at": "2026-08-17T00:00:00Z",
            "duration_ms": 1000,
            "status": "failed",
            "transcript_id": None,
            "error_code": "service_restart",
            "created_at": response.json()[0]["created_at"],
        }
    ]


def test_audio_chunk_pagination_uses_source_and_sequence_cursor(client, auth_headers):
    session = _create(client, auth_headers)
    client.post(
        f"/api/sessions/{session['id']}/start",
        json={"radio_mode": "both"},
        headers=auth_headers,
    )
    conn = db.get_db()
    try:
        for source, chunk_seq in (("mobile", 0), ("mobile", 1), ("pc", 0)):
            db.reserve_audio_chunk(
                conn,
                chunk_id=(
                    f"00000000-0000-4000-8000-{int(source == 'pc')}{chunk_seq:011d}"
                ),
                session_id=session["id"],
                source=source,
                codec="webm_opus",
                chunk_seq=chunk_seq,
                captured_at=f"2026-08-17T00:00:0{chunk_seq}+00:00",
                duration_ms=1000,
                content_sha256=f"{chunk_seq + (1 if source == 'pc' else 0):064x}",
            )
    finally:
        conn.close()

    first_page = client.get(
        f"/api/sessions/{session['id']}/audio-chunks?limit=2",
        headers=auth_headers,
    )
    assert first_page.status_code == 200
    assert [(item["source"], item["chunk_seq"]) for item in first_page.json()] == [
        ("mobile", 0),
        ("mobile", 1),
    ]

    second_page = client.get(
        f"/api/sessions/{session['id']}/audio-chunks"
        "?after_source=mobile&after_chunk_seq=1&limit=2",
        headers=auth_headers,
    )
    assert second_page.status_code == 200
    assert [(item["source"], item["chunk_seq"]) for item in second_page.json()] == [
        ("pc", 0)
    ]

    ambiguous_legacy_cursor = client.get(
        f"/api/sessions/{session['id']}/audio-chunks?after_chunk_seq=0",
        headers=auth_headers,
    )
    assert ambiguous_legacy_cursor.status_code == 422


def test_events_and_pagination(client, auth_headers):
    first = _create(client, auth_headers, "第一个")
    second = _create(client, auth_headers, "第二个")
    page = client.get("/api/sessions?limit=1&offset=0", headers=auth_headers)
    assert page.status_code == 200
    assert [item["id"] for item in page.json()] == [second["id"]]

    client.post(
        f"/api/sessions/{first['id']}/start",
        json={"radio_mode": "mobile"},
        headers=auth_headers,
    )
    events = client.get(
        f"/api/sessions/{first['id']}/events", headers=auth_headers
    ).json()
    assert [event["type"] for event in events] == ["session_state", "session_state"]
    after = client.get(
        f"/api/sessions/{first['id']}/events?after_event_id={events[0]['event_id']}",
        headers=auth_headers,
    ).json()
    assert [event["event_id"] for event in after] == [events[1]["event_id"]]


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions?offset=9223372036854775808",
        "/api/sessions/{session_id}/events?after_event_id=9223372036854775808",
        (
            "/api/sessions/{session_id}/audio-chunks"
            "?source=pc&after_chunk_seq=2147483648"
        ),
    ],
)
def test_oversized_integer_query_parameters_are_rejected(client, auth_headers, path):
    session = _create(client, auth_headers)
    response = client.get(path.format(session_id=session["id"]), headers=auth_headers)
    assert response.status_code == 422


def test_delete_session_cascades_all_data(client, auth_headers):
    session = _create(client, auth_headers, title="待删除")
    sid = session["id"]
    client.post(f"/api/sessions/{sid}/start", json={"radio_mode": "pc"}, headers=auth_headers)
    client.post(f"/api/sessions/{sid}/end", headers=auth_headers)

    response = client.delete(f"/api/sessions/{sid}", headers=auth_headers)
    assert response.status_code == 204

    assert client.get(f"/api/sessions/{sid}", headers=auth_headers).status_code == 404
    conn = db.get_db()
    try:
        for table in ("transcripts", "answers", "audio_chunks", "session_events", "reviews"):
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (sid,)
            ).fetchone()[0]
            assert count == 0, f"{table} 未级联清空"
    finally:
        conn.close()


def test_delete_recording_session_is_rejected(client, auth_headers):
    session = _create(client, auth_headers, title="进行中")
    client.post(
        f"/api/sessions/{session['id']}/start",
        json={"radio_mode": "pc"},
        headers=auth_headers,
    )
    response = client.delete(f"/api/sessions/{session['id']}", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["detail"]["current_status"] == "recording"


def test_delete_missing_session_returns_404(client, auth_headers):
    assert client.delete("/api/sessions/00000000-0000-0000-0000-000000000000", headers=auth_headers).status_code == 404
