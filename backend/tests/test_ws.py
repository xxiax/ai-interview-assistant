from __future__ import annotations

import asyncio
import base64
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect

from app import asr, db, llm
from app.ws import ConnectionManager, pipeline

AUTH_TOKEN = "test-access-token-0123456789abcdef0123456789"


@pytest.mark.asyncio
async def test_connection_manager_serializes_replay_and_deduplicates_events():
    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

        async def close(self, code=1000):
            return None

    manager = ConnectionManager()
    ws = FakeWebSocket()
    manager.connect("session", ws)

    async with manager.session_lock("session"):
        broadcast = asyncio.create_task(
            manager.broadcast("session", {"type": "answer", "event_id": 2})
        )
        await asyncio.sleep(0)
        assert ws.messages == []
        assert await manager.send(ws, {"type": "transcript", "event_id": 1})

    await broadcast
    assert [message["event_id"] for message in ws.messages] == [1, 2]
    assert await manager.send(ws, {"type": "transcript", "event_id": 1})
    assert [message["event_id"] for message in ws.messages] == [1, 2]


@pytest.mark.asyncio
async def test_connection_manager_fills_event_gaps_before_advancing_watermark():
    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    events = [
        {
            "event_id": 1,
            "session_id": "session",
            "type": "transcript",
            "created_at": "2026-08-18T00:00:00+00:00",
            "payload": {"text": "first"},
        },
        {
            "event_id": 2,
            "session_id": "session",
            "type": "answer",
            "created_at": "2026-08-18T00:00:01+00:00",
            "payload": {"answer": "second"},
        },
    ]

    async def load_events(session_id, after_event_id, limit):
        assert session_id == "session"
        return [event for event in events if event["event_id"] > after_event_id][:limit]

    manager = ConnectionManager(event_loader=load_events)
    ws = FakeWebSocket()
    manager.connect("session", ws)

    await manager.broadcast("session", {"type": "answer", "event_id": 2})
    await manager.broadcast("session", {"type": "transcript", "event_id": 1})

    assert [message["event_id"] for message in ws.messages] == [1, 2]
    assert manager.event_watermarks[ws] == 2


@pytest.mark.asyncio
async def test_connection_manager_releases_unused_session_locks():
    manager = ConnectionManager()

    async with manager.session_lock("session"):
        assert "session" in manager._session_locks

    assert manager._session_locks == {}


def _create_and_start(client, auth_headers, mode="pc"):
    session = client.post(
        "/api/sessions", json={"title": "WebSocket 测试"}, headers=auth_headers
    ).json()
    client.post(
        f"/api/sessions/{session['id']}/start",
        json={"radio_mode": mode},
        headers=auth_headers,
    )
    return session["id"]


def _authenticate(ws, last_event_id=0):
    ws.send_json(
        {
            "v": 1,
            "type": "authenticate",
            "token": AUTH_TOKEN,
            "last_event_id": last_event_id,
        }
    )
    messages = []
    while True:
        message = ws.receive_json()
        messages.append(message)
        if message["type"] == "sync_complete":
            return messages


def _audio_message(seq: int, text: str, *, chunk_id=None):
    return {
        "v": 1,
        "type": "audio_chunk",
        "chunk_id": chunk_id or str(uuid4()),
        "source": "pc",
        "codec": "webm_opus",
        "chunk_seq": seq,
        "captured_at": datetime(
            2026, 8, 17, 0, 0, seq, tzinfo=timezone.utc
        ).isoformat(),
        "duration_ms": 1000,
        "data": base64.b64encode(text.encode()).decode(),
    }


def test_ws_requires_authentication(client, auth_headers):
    session_id = _create_and_start(client, auth_headers)
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        ws.send_json({"v": 1, "type": "ping"})
        assert ws.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401


def test_browser_origin_is_denied_unless_allowlisted(client, auth_headers, monkeypatch):
    session_id = _create_and_start(client, auth_headers)
    with client.websocket_connect(
        f"/ws/{session_id}", headers={"origin": "https://evil.example"}
    ) as ws:
        assert ws.receive_json()["code"] == "origin_not_allowed"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4403

    monkeypatch.setenv("AI_ALLOWED_ORIGINS", "https://app.example")
    with client.websocket_connect(
        f"/ws/{session_id}", headers={"origin": "https://app.example"}
    ) as ws:
        assert _authenticate(ws)[-1]["type"] == "sync_complete"


def test_ws_rejects_wrong_token_and_unknown_session(client):
    with client.websocket_connect("/ws/missing") as ws:
        ws.send_json(
            {"v": 1, "type": "authenticate", "token": "wrong", "last_event_id": 0}
        )
        assert ws.receive_json()["code"] == "authentication_failed"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()

    with client.websocket_connect("/ws/missing") as ws:
        ws.send_json(
            {"v": 1, "type": "authenticate", "token": AUTH_TOKEN, "last_event_id": 0}
        )
        assert ws.receive_json()["code"] == "session_not_found"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4404


def test_out_of_order_chunks_are_transcribed_in_chunk_sequence(
    client, auth_headers, monkeypatch
):
    session_id = _create_and_start(client, auth_headers)

    async def fake_transcribe(audio_bytes, codec, source, declared_duration_ms):
        assert codec == "webm_opus"
        assert source == "pc"
        assert declared_duration_ms == 1000
        if audio_bytes == b"first":
            await asyncio.sleep(0.03)
        return audio_bytes.decode()

    monkeypatch.setattr(asr, "transcribe_audio", fake_transcribe)
    second = _audio_message(1, "second")
    first = _audio_message(0, "first")
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        _authenticate(ws)
        ws.send_json(second)
        ws.send_json(first)
        transcripts = []
        while len(transcripts) < 2:
            message = ws.receive_json()
            if message["type"] == "transcript":
                transcripts.append(message)
        assert [item["chunk_seq"] for item in transcripts] == [0, 1]
        assert [item["text"] for item in transcripts] == ["first", "second"]

        ws.send_json(first)
        while True:
            duplicate = ws.receive_json()
            if duplicate["type"] == "chunk_ack" and duplicate.get("duplicate"):
                break
        assert duplicate["status"] == "done"

        changed_payload = {**first, "data": base64.b64encode(b"changed").decode()}
        ws.send_json(changed_payload)
        while True:
            conflict = ws.receive_json()
            if conflict["type"] == "error" and conflict.get("code") == "invalid_audio_chunk":
                break
        assert conflict["code"] == "invalid_audio_chunk"


def test_audio_backpressure_and_size_limit(client, auth_headers, monkeypatch):
    session_id = _create_and_start(client, auth_headers)
    monkeypatch.setattr(pipeline, "audio_queue_size", 1)
    monkeypatch.setenv("AI_MAX_AUDIO_CHUNK_BYTES", "2")

    async def slow_transcribe(*_args):
        await asyncio.sleep(0.3)
        return "ok"

    monkeypatch.setattr(asr, "transcribe_audio", slow_transcribe)
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        _authenticate(ws)
        ws.send_json(_audio_message(0, "a"))
        assert ws.receive_json()["status"] == "queued"
        ws.send_json(_audio_message(1, "b"))
        while True:
            response = ws.receive_json()
            if response["type"] == "error":
                break
        assert response["code"] == "audio_backpressure"

        ws.send_json(_audio_message(1, "abc"))
        while True:
            too_large = ws.receive_json()
            if too_large["type"] == "error":
                break
        assert too_large["code"] == "invalid_audio"


def test_audio_sequence_conflict_identifies_rejected_chunk(
    client, auth_headers, monkeypatch
):
    session_id = _create_and_start(client, auth_headers)

    async def slow_transcribe(*_args, **_kwargs):
        await asyncio.sleep(0.2)
        return "ok"

    monkeypatch.setattr(asr, "transcribe_audio", slow_transcribe)
    first = _audio_message(0, "first")
    conflicting = _audio_message(0, "conflicting")

    with client.websocket_connect(f"/ws/{session_id}") as ws:
        _authenticate(ws)
        ws.send_json(first)
        while True:
            accepted = ws.receive_json()
            if (
                accepted.get("type") == "chunk_ack"
                and accepted.get("chunk_id") == first["chunk_id"]
            ):
                break

        ws.send_json(conflicting)
        while True:
            rejected = ws.receive_json()
            if rejected.get("type") == "error":
                break

        assert rejected["code"] == "invalid_audio_chunk"
        assert "chunk_seq" in rejected["message"]
        assert rejected["chunk_id"] == conflicting["chunk_id"]


def test_event_replay_and_cursor_is_capped(client, auth_headers):
    session_id = _create_and_start(client, auth_headers, mode="mobile")
    client.post(f"/api/sessions/{session_id}/end", headers=auth_headers)
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        messages = _authenticate(ws, last_event_id=999999)
    sync = messages[-1]
    assert sync["type"] == "sync_complete"
    assert sync["latest_event_id"] < 999999
    assert sync["status"] == "ended"
    assert sync["radio_mode"] == "mobile"

    with client.websocket_connect(f"/ws/{session_id}") as ws:
        replayed = _authenticate(ws)
    states = [message for message in replayed if message["type"] == "session_state"]
    assert [state["status"] for state in states] == ["idle", "recording", "ended"]


def test_ended_session_rejects_ws_mutation(client, auth_headers):
    session_id = _create_and_start(client, auth_headers)
    client.post(f"/api/sessions/{session_id}/end", headers=auth_headers)
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        _authenticate(ws)
        ws.send_json({"v": 1, "type": "set_radio_mode", "mode": "both"})
        error = ws.receive_json()
        assert error["code"] == "invalid_session_state"
        assert error["current_status"] == "ended"


def test_ws_control_messages_and_regenerate_answer(client, auth_headers, monkeypatch):
    session = client.post(
        "/api/sessions", json={"title": "控制消息"}, headers=auth_headers
    ).json()

    async def fake_stream(question, context=""):
        yield llm.LLMStreamPart(text=f"回答:{question}:{context}")

    monkeypatch.setattr(llm, "stream_answer", fake_stream)
    with client.websocket_connect(f"/ws/{session['id']}") as ws:
        initial = _authenticate(ws)
        assert initial[-1]["status"] == "idle"

        ws.send_text("{")
        assert ws.receive_json()["code"] == "invalid_message"
        ws.send_json({"v": 1, "type": "ping"})
        assert ws.receive_json()["type"] == "pong"

        ws.send_json({"v": 1, "type": "start_session", "radio_mode": "pc"})
        assert ws.receive_json()["status"] == "recording"
        ws.send_json({"v": 1, "type": "set_radio_mode", "mode": "mobile"})
        assert ws.receive_json()["radio_mode"] == "mobile"

        rejected = _audio_message(0, "not-allowed")
        ws.send_json(rejected)
        source_error = ws.receive_json()
        assert source_error["code"] == "source_not_allowed"
        assert source_error["chunk_id"] == rejected["chunk_id"]

        ws.send_json(
            {
                "v": 1,
                "type": "regenerate_answer",
                "question": "测试问题",
                "use_search": False,
            }
        )
        stream_seen = False
        while True:
            answer = ws.receive_json()
            if answer["type"] == "answer_stream":
                stream_seen = True
            if answer["type"] == "answer":
                break
        assert stream_seen
        assert answer["source"] == "llm"
        assert answer["question"] == "测试问题"

        ws.send_json({"v": 1, "type": "resume", "after_event_id": answer["event_id"]})
        assert ws.receive_json()["type"] == "sync_complete"
        ws.send_json({"v": 1, "type": "end_session"})
        assert ws.receive_json()["status"] == "ended"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_ws_cancel_audio_source_is_broadcast_and_idempotent(
    client, auth_headers, monkeypatch
):
    session_id = _create_and_start(client, auth_headers)

    async def slow_transcribe(*_args):
        await asyncio.sleep(10)
        return "不应完成"

    monkeypatch.setattr(asr, "transcribe_audio", slow_transcribe)
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        _authenticate(ws)
        ws.send_json(_audio_message(0, "blocked"))
        while True:
            queued = ws.receive_json()
            if queued.get("type") == "chunk_ack" and queued.get("status") == "queued":
                break

        cancel = {
            "v": 1,
            "type": "cancel_audio_source",
            "source": "pc",
            "through_chunk_seq": 0,
            "reason": "capture_stopped",
        }
        ws.send_json(cancel)
        while True:
            cancelled = ws.receive_json()
            if (
                cancelled.get("type") == "chunk_ack"
                and cancelled.get("status") == "cancelled"
            ):
                break
        assert cancelled["chunk_seq"] == 0
        assert cancelled["error_code"] == "capture_stopped"

        ws.send_json(cancel)
        ws.send_json({"v": 1, "type": "ping"})
        assert ws.receive_json()["type"] == "pong"

        conn = db.get_db()
        try:
            events = db.get_events(conn, session_id, 0, 200)
        finally:
            conn.close()
        cancelled_events = [
            event
            for event in events
            if event["type"] == "chunk_ack"
            and event["payload"]["status"] == "cancelled"
        ]
        assert len(cancelled_events) == 1

        ws.send_json({"v": 1, "type": "end_session"})
        assert ws.receive_json()["status"] == "ended"


def test_switching_to_mobile_cancels_pc_and_higher_seq_resumes_when_reenabled(
    client, auth_headers, monkeypatch
):
    session_id = _create_and_start(client, auth_headers)

    async def fake_transcribe(audio_bytes, *_args):
        if audio_bytes == b"blocked":
            await asyncio.sleep(10)
            return "不应完成"
        return audio_bytes.decode()

    monkeypatch.setattr(asr, "transcribe_audio", fake_transcribe)
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        _authenticate(ws)
        ws.send_json(_audio_message(0, "blocked"))
        while True:
            queued = ws.receive_json()
            if queued.get("type") == "chunk_ack" and queued.get("status") == "queued":
                break

        ws.send_json({"v": 1, "type": "set_radio_mode", "mode": "mobile"})
        saw_mobile = False
        saw_cancelled = False
        while not (saw_mobile and saw_cancelled):
            message = ws.receive_json()
            saw_mobile = saw_mobile or (
                message.get("type") == "session_state"
                and message.get("radio_mode") == "mobile"
            )
            saw_cancelled = saw_cancelled or (
                message.get("type") == "chunk_ack"
                and message.get("status") == "cancelled"
                and message.get("error_code") == "source_disabled"
            )

        rejected = _audio_message(1, "still-disabled")
        ws.send_json(rejected)
        while True:
            source_error = ws.receive_json()
            if source_error.get("type") == "error":
                break
        assert source_error["code"] == "source_not_allowed"

        ws.send_json({"v": 1, "type": "set_radio_mode", "mode": "pc"})
        while True:
            state = ws.receive_json()
            if state.get("type") == "session_state":
                break
        assert state["radio_mode"] == "pc"

        ws.send_json(_audio_message(1, "after"))
        while True:
            transcript = ws.receive_json()
            if transcript.get("type") == "transcript":
                break
        assert (transcript["chunk_seq"], transcript["text"]) == (1, "after")

        conn = db.get_db()
        try:
            chunks = db.get_audio_chunks(conn, session_id)
            transcripts = db.get_transcripts(conn, session_id)
        finally:
            conn.close()
        assert [(chunk["chunk_seq"], chunk["status"]) for chunk in chunks] == [
            (0, "cancelled"),
            (1, "done"),
        ]
        assert [(item["chunk_seq"], item["text"]) for item in transcripts] == [
            (1, "after")
        ]

        ws.send_json({"v": 1, "type": "end_session"})
        while True:
            ended = ws.receive_json()
            if ended.get("type") == "session_state":
                break
        assert ended["status"] == "ended"


def test_missing_asr_config_reports_clear_error_without_retry(
    client, auth_headers, monkeypatch
):
    """未配置 ASR 时:错误消息必须指明配置缺失,且分片标为不可重试的 config_error。"""
    session_id = _create_and_start(client, auth_headers)

    async def no_key_transcribe(*_args, **_kwargs):
        raise RuntimeError("未配置 ASR：请在客户端设置页保存 Groq API Key")

    monkeypatch.setattr(asr, "transcribe_audio", no_key_transcribe)
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        _authenticate(ws)
        ws.send_json(_audio_message(0, "silence"))
        saw_error = False
        saw_ack = False
        for _ in range(6):
            message = ws.receive_json()
            if message["type"] == "error" and message.get("code") == "audio_processing_failed":
                assert "ASR" in message["message"] or "配置" in message["message"], (
                    "通用错误消息必须包含真实原因"
                )
                saw_error = True
            if message["type"] == "chunk_ack" and message.get("status") == "failed":
                saw_ack = True
            if saw_error and saw_ack:
                break
        assert saw_error, "必须收到指明原因的错误"
        assert saw_ack, "分片必须标为 failed"


def test_funasr_auth_error_is_config_class_not_retryable(
    client, auth_headers, monkeypatch
):
    """FunASR 401（不含 groq.com 字样）必须归类为不可重试的 config_missing。

    B4 回归：此前靠字符串 "401 ... groq.com" 判定鉴权失败，FunASR 的
    401 会被误标为可重试的 processing_failed，客户端会无限重试。
    """
    session_id = _create_and_start(client, auth_headers)

    async def funasr_401(*_args, **_kwargs):
        raise asr.AsrAuthError("FunASR 拒绝连接(HTTP 401，token 无效或无权限)", 401)

    monkeypatch.setattr(asr, "transcribe_audio", funasr_401)
    with client.websocket_connect(f"/ws/{session_id}") as ws:
        _authenticate(ws)
        ws.send_json(_audio_message(0, "silence"))
        error_codes = []
        ack_status = None
        for _ in range(6):
            message = ws.receive_json()
            if message["type"] == "error":
                error_codes.append((message.get("code"), message.get("message")))
            if message["type"] == "chunk_ack":
                ack_status = (message.get("status"), message.get("error_code"))
            if ack_status and error_codes:
                break
        assert ack_status is not None, "必须收到分片终态 ACK"
        assert ack_status == ("failed", "config_missing"), (
            "FunASR 鉴权失败应标为不可重试的 config_missing"
        )
        assert any(
            code == "audio_processing_failed" and ("API Key" in msg or "Token" in msg)
            for code, msg in error_codes
        ), f"错误消息必须指明鉴权问题，实际: {error_codes}"


def test_gap_timeout_advances_expected_sequence(client, auth_headers, monkeypatch):
    """洞被放弃后 worker 必须推进 expected_seq,不能永远等同一个洞刷屏。"""
    session_id = _create_and_start(client, auth_headers)

    async def fake_transcribe(audio_bytes, codec, source, declared_duration_ms):
        return audio_bytes.decode()

    monkeypatch.setattr(asr, "transcribe_audio", fake_transcribe)
    monkeypatch.setenv("AI_AUDIO_REORDER_WAIT_SECONDS", "0.2")

    with client.websocket_connect(f"/ws/{session_id}") as ws:
        _authenticate(ws)
        # 只发 seq=2(0/1 为洞,模拟被客户端放弃):应报一次缺口并标 failed
        ws.send_json(_audio_message(2, "orphan"))
        gap_seen = False
        deadline = time.time() + 6
        while time.time() < deadline and not gap_seen:
            try:
                msg = ws.receive_json()
            except Exception:  # noqa: BLE001 - TestClient 断开可能包装为多种异常
                break
            if msg.get("type") == "error" and msg.get("code") == "audio_sequence_gap":
                gap_seen = True
        assert gap_seen, "第一次缺口应报错"

        # 发 seq=3:第一次缺口后 worker 仍在洞头等补传;第二次超时判定洞被放弃,
        # 应把 3 重新入队转写(而不是永远卡在洞上刷 gap 报错)
        ws.send_json(_audio_message(3, "after"))
        # 等待重入队+转写完成,以 DB 终态断言(TestClient 下 broadcast 不可靠)
        deadline = time.time() + 8
        done = False
        while time.time() < deadline and not done:
            time.sleep(0.2)
            conn = db.get_db()
            try:
                row = conn.execute(
                    "SELECT status FROM audio_chunks WHERE session_id=? AND chunk_seq=3",
                    (session_id,),
                ).fetchone()
            finally:
                conn.close()
            done = bool(row and row["status"] == "done")
        assert done, "洞后的分片应完成转写(done),而非永远卡在缺口上"
