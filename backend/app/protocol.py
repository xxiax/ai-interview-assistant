"""版本化 WebSocket 协议与严格消息解析。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, ValidationError, field_validator

from .db import CHUNK_SEQ_MAX, SQLITE_INT_MAX
from .models import AudioCodec, AudioSource, RadioMode, StrictModel

PROTOCOL_VERSION = 1


class AuthenticateMessage(StrictModel):
    # v 必填：两个客户端（Tauri ws_client/lib）都显式发送 v:1，
    # 不接受省略版本的客户端
    v: Literal[1]
    type: Literal["authenticate"]
    token: Annotated[str, Field(min_length=1, max_length=4096)]
    last_event_id: Annotated[int, Field(ge=0, le=SQLITE_INT_MAX)] = 0


class StartSessionMessage(StrictModel):
    v: Literal[1] = PROTOCOL_VERSION
    type: Literal["start_session"]
    radio_mode: RadioMode = "pc"


class SetRadioModeMessage(StrictModel):
    v: Literal[1] = PROTOCOL_VERSION
    type: Literal["set_radio_mode"]
    mode: RadioMode


class AudioChunkMessage(StrictModel):
    v: Literal[1] = PROTOCOL_VERSION
    type: Literal["audio_chunk"]
    chunk_id: UUID
    source: AudioSource
    codec: AudioCodec
    # strict=True：与 through_chunk_seq 对齐，拒绝字符串序号
    chunk_seq: Annotated[int, Field(strict=True, ge=0, le=CHUNK_SEQ_MAX)]
    captured_at: datetime
    duration_ms: Annotated[int, Field(ge=100, le=10_000)]
    data: Annotated[str, Field(min_length=1)]

    @field_validator("captured_at")
    @classmethod
    def captured_at_requires_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at 必须包含时区")
        return value


class CancelAudioSourceMessage(StrictModel):
    v: Literal[1] = PROTOCOL_VERSION
    type: Literal["cancel_audio_source"]
    source: AudioSource
    through_chunk_seq: Annotated[
        int, Field(strict=True, ge=0, le=CHUNK_SEQ_MAX)
    ]
    reason: Literal["capture_stopped", "source_disabled"]


class RegenerateAnswerMessage(StrictModel):
    v: Literal[1] = PROTOCOL_VERSION
    type: Literal["regenerate_answer"]
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    use_search: bool = False


class EndSessionMessage(StrictModel):
    v: Literal[1] = PROTOCOL_VERSION
    type: Literal["end_session"]


class ResumeMessage(StrictModel):
    v: Literal[1] = PROTOCOL_VERSION
    type: Literal["resume"]
    after_event_id: Annotated[int, Field(ge=0, le=SQLITE_INT_MAX)]


class PingMessage(StrictModel):
    v: Literal[1] = PROTOCOL_VERSION
    type: Literal["ping"]


CLIENT_MESSAGE_MODELS = {
    "start_session": StartSessionMessage,
    "set_radio_mode": SetRadioModeMessage,
    "audio_chunk": AudioChunkMessage,
    "cancel_audio_source": CancelAudioSourceMessage,
    "regenerate_answer": RegenerateAnswerMessage,
    "end_session": EndSessionMessage,
    "resume": ResumeMessage,
    "ping": PingMessage,
}


def parse_auth_message(data: object) -> AuthenticateMessage:
    if not isinstance(data, dict):
        raise ValueError("认证消息必须是 JSON 对象")  # noqa: TRY004 - 协议解析统一返回 ValueError
    try:
        return AuthenticateMessage.model_validate(data)
    except ValidationError as exc:
        raise ValueError("认证消息格式错误") from exc


def parse_client_message(data: object):
    if not isinstance(data, dict):
        raise ValueError("消息必须是 JSON 对象")  # noqa: TRY004 - 协议解析统一返回 ValueError
    message_type = data.get("type")
    model = CLIENT_MESSAGE_MODELS.get(message_type)
    if model is None:
        raise ValueError("未知消息类型")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ValueError("消息字段格式错误") from exc


def server_message(message_type: str, **payload) -> dict:
    return {"v": PROTOCOL_VERSION, "type": message_type, **payload}


def event_message(event: dict) -> dict:
    """把持久化事件行转换为对外的服务端消息。

    事件 payload 由各写路径构造，个别路径可能重复携带信封字段；
    统一在此剔除，保证 envelope（v/type/event_id/session_id/created_at）
    只来自事件行本身。realtime 与 ws 此前各有一份实现且行为不一致，
    现收敛为唯一公共实现。
    """
    payload = dict(event["payload"])
    for envelope_field in ("v", "type", "event_id", "session_id", "created_at"):
        payload.pop(envelope_field, None)
    return server_message(
        event["type"],
        event_id=event["event_id"],
        session_id=event["session_id"],
        created_at=event["created_at"],
        **payload,
    )
