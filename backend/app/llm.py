"""LLM 客户端（OpenAI 兼容格式）。"""
import sqlite3

import httpx

from . import db


def _get_llm_config() -> dict:
    """获取当前启用的 LLM 配置，未配置则抛错。"""
    conn = db.get_db()
    try:
        config = db.get_active_config(conn, "llm")
    except sqlite3.OperationalError:
        # 数据库尚未初始化（缺少 configs 表），视为未配置
        config = None
    finally:
        conn.close()
    if not config:
        raise RuntimeError("未配置 LLM，请在设置页配置")
    return config["data"]


async def _chat(messages: list[dict], temperature: float = 0.7) -> str:
    """调用 OpenAI 兼容的 chat completions 接口。"""
    config = _get_llm_config()
    base_url = config.get("base_url", "").rstrip("/")
    api_key = config.get("api_key", "")
    model = config.get("model", "")
    auth_field = config.get("auth_field", "Authorization")

    if not base_url or not api_key or not model:
        raise RuntimeError("LLM 配置不完整：需要 base_url、api_key、model")

    headers = {auth_field: f"Bearer {api_key}"}
    if auth_field.lower() == "authorization":
        headers[auth_field] = f"Bearer {api_key}"

    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def generate_answer(question: str, context: str = "") -> str:
    """根据面试问题生成回答要点。"""
    system = (
        "你是一名资深面试辅导专家。请根据面试官的问题，给出简洁、有条理的回答要点。"
        "回答要点应包含：核心答案、关键点、可能的追问方向。"
        "使用中文回答，控制在 200 字以内。"
    )
    user = f"面试官的问题：{question}"
    if context:
        user += f"\n\n面试上下文（供参考）：{context}"
    return await _chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.5,
    )


async def generate_review(transcripts: list[dict], answers: list[dict]) -> str:
    """根据完整转写和答案生成复盘报告。"""
    system = (
        "你是一名资深面试辅导专家。请根据面试的完整转写和 AI 生成的答案，"
        "生成一份复盘报告，包含：1. 面试问题清单 2. 每个问题的回答评估 3. 改进建议。"
        "使用中文回答。"
    )
    transcript_text = "\n".join(f"[{t['source']}] {t['text']}" for t in transcripts)
    answer_text = "\n".join(f"Q: {a['question']}\nA: {a['answer']}" for a in answers)
    user = f"面试转写：\n{transcript_text}\n\nAI 答案：\n{answer_text}"
    return await _chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.4,
    )
