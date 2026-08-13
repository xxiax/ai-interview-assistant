"""ASR 客户端（Groq Whisper API）。"""
import os

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"


def _get_api_key() -> str:
    """获取 Groq API key。"""
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 GROQ_API_KEY 环境变量")
    return key


async def transcribe_audio(audio_bytes: bytes, source: str = "pc") -> str:
    """调用 Groq Whisper 转写音频分片。"""
    api_key = _get_api_key()
    files = {"file": ("chunk.webm", audio_bytes, "audio/webm")}
    data = {"model": GROQ_MODEL, "language": "zh"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("text", "")
