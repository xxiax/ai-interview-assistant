"""REST API 的显式请求与响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from .security import normalize_openai_base_url, validate_auth_field

RadioMode = Literal["pc", "mobile", "both"]
AudioSource = Literal["pc", "mobile"]
SessionStatus = Literal["idle", "recording", "ended"]
AudioCodec = Literal["webm_opus", "ogg_opus", "m4a_aac", "wav_pcm_s16le"]
ReasoningEffort = Literal["low", "medium", "high"]
GROQ_DEFAULT_MODEL = "whisper-large-v3"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateSessionRequest(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]


class StartSessionRequest(StrictModel):
    radio_mode: RadioMode = "pc"


class SessionResponse(StrictModel):
    id: str
    title: str
    status: SessionStatus
    radio_mode: RadioMode
    created_at: datetime
    ended_at: datetime | None = None


class TranscriptResponse(StrictModel):
    id: int
    session_id: str
    source: AudioSource
    text: str
    timestamp: datetime
    seq: int
    chunk_id: str | None = None
    chunk_seq: int | None = None
    captured_at: datetime | None = None


class AudioChunkResponse(StrictModel):
    chunk_id: str
    session_id: str
    source: AudioSource
    codec: AudioCodec
    chunk_seq: int
    captured_at: datetime
    duration_ms: int
    status: Literal["queued", "done", "failed", "cancelled"]
    transcript_id: int | None = None
    error_code: str | None = None
    created_at: datetime


class AnswerResponse(StrictModel):
    id: int
    session_id: str
    question: str
    answer: str
    source: Literal["llm", "search+llm"]
    created_at: datetime


class LLMConfigData(StrictModel):
    base_url: Annotated[str, Field(min_length=1, max_length=2000)]
    # 编辑模式(config_id 存在)允许空串表示沿用已存密钥;新增模式由
    # routes_configs._serialized_config_data 拒空,保持原有校验强度。
    api_key: Annotated[SecretStr, Field(max_length=4096)]
    model: Annotated[str, Field(min_length=1, max_length=200)]
    auth_field: str = "Authorization"
    # 低强度优先降低实时回答延迟；不支持该参数的模型由运行时自动省略。
    reasoning_effort: ReasoningEffort = "low"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_openai_base_url(value)

    @field_validator("auth_field")
    @classmethod
    def validate_header(cls, value: str) -> str:
        return validate_auth_field(value)


class LlmModelsProbeRequest(StrictModel):
    """客户端"获取模型"按钮的探测请求；api_key 为空表示复用已激活配置的 Key。"""

    base_url: Annotated[str, Field(min_length=1, max_length=2000)]
    api_key: Annotated[str, Field(max_length=4096)] = ""
    auth_field: str = "Authorization"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_openai_base_url(value)

    @field_validator("auth_field")
    @classmethod
    def validate_header(cls, value: str) -> str:
        return validate_auth_field(value)


class LlmModelsProbeResponse(StrictModel):
    models: list[str]


class SearchConfigData(StrictModel):
    # Bing Web Search API 已于 2025-08 退役，不再接受新配置；
    # 数据库里已存在的 bing 配置在运行时按不可用降级（见 search.py）。
    engine: Literal["google"]
    api_key: Annotated[SecretStr, Field(min_length=1, max_length=4096)]
    cx: Annotated[str, Field(max_length=300)] = ""

    @field_validator("engine", mode="before")
    @classmethod
    def reject_retired_bing(cls, value: object) -> object:
        if isinstance(value, str) and value.lower() == "bing":
            raise ValueError("Bing Web Search API 已于 2025-08 退役，请改用 Google")
        return value

    @model_validator(mode="after")
    def google_requires_cx(self):
        if not self.cx:
            raise ValueError("Google 搜索配置必须提供 cx")
        return self


class ASRConfigData(StrictModel):
    api_key: Annotated[SecretStr, Field(min_length=1, max_length=4096)]
    model: Annotated[str, Field(min_length=1, max_length=100)] = GROQ_DEFAULT_MODEL


class NetworkConfigData(StrictModel):
    """网络代理配置:proxy_url 支持 http/https/socks5/socks5h。"""
    proxy_url: Annotated[str, Field(min_length=1, max_length=500)]
    api_key: Annotated[SecretStr, Field(min_length=1, max_length=4096)]

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_scheme(cls, value: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
            raise ValueError("proxy_url 必须是 http/https/socks5/socks5h 之一且包含主机")
        return value


class SaveConfigRequest(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    data: LLMConfigData | SearchConfigData | ASRConfigData | NetworkConfigData
    is_active: bool = False
    # 编辑模式:携带已存在配置的 id 时走更新(空 api_key 表示沿用旧密钥);
    # 缺省时保持原有"新增"语义,老客户端不受影响。
    config_id: Annotated[int, Field(ge=1, le=10**15)] | None = None


class ConfigResponse(StrictModel):
    id: int
    type: Literal["llm", "search", "asr", "network"]
    name: str
    data: dict
    is_active: bool
    secret_configured: bool


class PromptResponse(StrictModel):
    prompt: str


class UpdatePromptRequest(StrictModel):
    prompt: Annotated[str, Field(max_length=8000)] = ""


class ReviewRequest(StrictModel):
    use_search: bool = False


class ReviewResponse(StrictModel):
    id: int
    session_id: str
    content: str
    source: Literal["llm", "search+llm"]
    created_at: datetime


class EventResponse(StrictModel):
    event_id: int
    session_id: str
    type: str
    created_at: datetime
    payload: dict
