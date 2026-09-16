from __future__ import annotations

import pytest

from app.db import CHUNK_SEQ_MAX
from app.protocol import (
    event_message,
    parse_auth_message,
    parse_client_message,
    server_message,
)


def test_protocol_rejects_unknown_type_extra_fields_and_naive_time():
    with pytest.raises(ValueError, match="未知"):
        parse_client_message({"v": 1, "type": "unknown"})
    with pytest.raises(ValueError, match="字段"):
        parse_client_message({"v": 1, "type": "ping", "extra": True})
    with pytest.raises(ValueError, match="字段"):
        parse_client_message(
            {
                "v": 1,
                "type": "audio_chunk",
                "chunk_id": "76aa92c8-28cc-4fd7-a818-54ec457305f5",
                "source": "pc",
                "codec": "webm_opus",
                "chunk_seq": 0,
                "captured_at": "2026-08-17T00:00:00",
                "duration_ms": 1000,
                "data": "YQ==",
            }
        )


def test_auth_parser_and_server_version():
    auth = parse_auth_message(
        {"v": 1, "type": "authenticate", "token": "x", "last_event_id": 2}
    )
    assert auth.last_event_id == 2
    assert server_message("pong") == {"v": 1, "type": "pong"}
    with pytest.raises(ValueError, match="对象"):
        parse_auth_message("not-an-object")


def test_cancel_audio_source_message_is_strict_and_versioned():
    message = parse_client_message(
        {
            "v": 1,
            "type": "cancel_audio_source",
            "source": "pc",
            "through_chunk_seq": 12,
            "reason": "capture_stopped",
        }
    )

    assert message.source == "pc"
    assert message.through_chunk_seq == 12
    assert message.reason == "capture_stopped"


def test_speech_end_message_carries_strict_sequence_boundary():
    message = parse_client_message(
        {
            "v": 1,
            "type": "speech_end",
            "source": "pc",
            "through_chunk_seq": 12,
        }
    )

    assert message.source == "pc"
    assert message.through_chunk_seq == 12


@pytest.mark.parametrize("through_chunk_seq", [-1, CHUNK_SEQ_MAX + 1, "0", True])
def test_speech_end_message_rejects_invalid_boundary(through_chunk_seq):
    with pytest.raises(ValueError, match="字段"):
        parse_client_message(
            {
                "v": 1,
                "type": "speech_end",
                "source": "pc",
                "through_chunk_seq": through_chunk_seq,
            }
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "browser"},
        {"through_chunk_seq": -1},
        {"through_chunk_seq": CHUNK_SEQ_MAX + 1},
        {"through_chunk_seq": "0"},
        {"through_chunk_seq": True},
        {"reason": "session_ended"},
        {"extra": True},
    ],
)
def test_cancel_audio_source_message_rejects_invalid_fields(overrides):
    payload = {
        "v": 1,
        "type": "cancel_audio_source",
        "source": "mobile",
        "through_chunk_seq": 0,
        "reason": "source_disabled",
        **overrides,
    }

    with pytest.raises(ValueError, match="字段"):
        parse_client_message(payload)


@pytest.mark.parametrize(
    "message",
    [
        {
            "v": 1,
            "type": "authenticate",
            "token": "x",
            "last_event_id": 9_223_372_036_854_775_808,
        },
        {
            "v": 1,
            "type": "resume",
            "after_event_id": 9_223_372_036_854_775_808,
        },
    ],
)
def test_protocol_rejects_event_cursors_larger_than_sqlite_integer(message):
    parser = (
        parse_auth_message
        if message["type"] == "authenticate"
        else parse_client_message
    )
    with pytest.raises(ValueError, match="格式|字段"):
        parser(message)


def _audio_chunk_payload(**overrides):
    payload = {
        "v": 1,
        "type": "audio_chunk",
        "chunk_id": "76aa92c8-28cc-4fd7-a818-54ec457305f5",
        "source": "pc",
        "codec": "webm_opus",
        "chunk_seq": 0,
        "captured_at": "2026-08-17T00:00:00+00:00",
        "duration_ms": 1000,
        "data": "YQ==",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("chunk_seq", ["0", True, 0.5])
def test_audio_chunk_seq_must_be_strict_integer(chunk_seq):
    """字符串/布尔/浮点序号必须拒绝（对齐 through_chunk_seq 的 strict 行为）。"""
    with pytest.raises(ValueError, match="字段"):
        parse_client_message(_audio_chunk_payload(chunk_seq=chunk_seq))


def test_authenticate_version_is_required():
    """v 必填：两个客户端都显式发 v:1，省略版本的消息应被拒绝。"""
    with pytest.raises(ValueError, match="格式"):
        parse_auth_message({"type": "authenticate", "token": "x"})


def test_event_message_strips_envelope_fields_from_payload():
    """公共 event_message：payload 中误带的信封字段必须剔除，信封以事件行为准。

    收敛前 realtime 与 ws 各有一份实现且行为不同（realtime 版不剔除），
    统一后按更严格的 ws 版本执行。
    """
    event = {
        "event_id": 7,
        "session_id": "s1",
        "type": "chunk_ack",
        "created_at": "2026-08-20T00:00:00+00:00",
        "payload": {
            "chunk_id": "abc",
            "status": "done",
            # 写路径误带的信封字段
            "v": 99,
            "type": "wrong",
            "session_id": "wrong-session",
        },
    }
    message = event_message(event)
    assert message["v"] == 1
    assert message["type"] == "chunk_ack"
    assert message["event_id"] == 7
    assert message["session_id"] == "s1"
    assert message["chunk_id"] == "abc"
    assert message["status"] == "done"
    assert "wrong" not in str(message)


def test_solve_screenshot_message_defaults_and_strictness():
    message = parse_client_message(
        {"v": 1, "type": "solve_screenshot", "image": "aGk="}
    )
    assert message.mime == "image/png"
    assert message.note == ""

    with_note = parse_client_message(
        {
            "v": 1,
            "type": "solve_screenshot",
            "image": "aGk=",
            "mime": "image/jpeg",
            "note": "只解第二题",
        }
    )
    assert with_note.mime == "image/jpeg"
    assert with_note.note == "只解第二题"


@pytest.mark.parametrize(
    "overrides",
    [
        {"image": ""},
        # 只放行 PNG/JPEG：其他容器多模态网关支持面参差不齐。
        {"mime": "image/gif"},
        {"mime": "application/pdf"},
        {"note": "x" * 501},
        {"v": 2},
        {"extra": True},
    ],
)
def test_solve_screenshot_message_rejects_invalid_fields(overrides):
    payload = {"v": 1, "type": "solve_screenshot", "image": "aGk="}
    payload.update(overrides)
    with pytest.raises(ValueError, match="字段"):
        parse_client_message(payload)
