"""付费外部调用的持久化预算和单 worker 全局并发边界。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import threading
from contextlib import asynccontextmanager

from . import db
from .security import get_auth_token

_LIMITS = {
    "llm_tokens": (
        (60, "AI_LLM_TOKENS_PER_MINUTE", 60_000),
        (3600, "AI_LLM_TOKENS_PER_HOUR", 300_000),
        (86_400, "AI_LLM_TOKENS_PER_DAY", 1_000_000),
    ),
    "search_requests": (
        (60, "AI_SEARCH_REQUESTS_PER_MINUTE", 30),
        (3600, "AI_SEARCH_REQUESTS_PER_HOUR", 300),
        (86_400, "AI_SEARCH_REQUESTS_PER_DAY", 2_000),
    ),
    # ASR 用量按毫秒入桶，环境变量仍按秒配置，换算见 _UNIT_SCALE。
    # 分片可以短到 100 ms，按秒向上取整会把一分钟连续说话记成 150 秒，
    # AI_ASR_SECONDS_PER_MINUTE 在真正说满一分钟之前就先爆了。
    "asr_millis": (
        (60, "AI_ASR_SECONDS_PER_MINUTE", 300),
        (3600, "AI_ASR_SECONDS_PER_HOUR", 3_600),
        (86_400, "AI_ASR_SECONDS_PER_DAY", 14_400),
    ),
}
# 环境变量单位到入桶单位的换算系数。只有需要亚秒精度的服务才在这里出现。
_UNIT_SCALE = {"asr_millis": 1_000}
_CONCURRENCY_ENV = {
    "llm": ("AI_LLM_MAX_CONCURRENCY", 4),
    "search": ("AI_SEARCH_MAX_CONCURRENCY", 2),
    "asr": ("AI_ASR_MAX_CONCURRENCY", 2),
}
_gates: dict[str, tuple[asyncio.AbstractEventLoop, int, asyncio.Semaphore]] = {}
_gates_lock = threading.Lock()


class PaidCallBusyError(RuntimeError):
    def __init__(self, service: str):
        super().__init__(f"{service} 付费调用并发已满")
        self.service = service


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数") from exc
    if value <= 0:
        raise RuntimeError(f"{name} 必须大于 0")
    return value


def _principals(service: str, provider_credential: str) -> list[str]:
    token = get_auth_token().encode("utf-8")
    token_id = hashlib.sha256(token).hexdigest()
    credential_id = hmac.new(
        token,
        f"{service}:{provider_credential}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return [f"token:{token_id}", f"credential:{service}:{credential_id}"]


def _reserve_sync(service: str, provider_credential: str, amount: int) -> None:
    conn = db.get_db()
    try:
        scale = _UNIT_SCALE.get(service, 1)
        limits = [
            (window, _positive_int(env_name, default) * scale)
            for window, env_name, default in _LIMITS[service]
        ]
        db.reserve_usage(
            conn,
            _principals(service, provider_credential),
            service,
            amount,
            limits,
        )
    finally:
        conn.close()


async def reserve_llm_tokens(provider_credential: str, amount: int) -> None:
    await asyncio.to_thread(_reserve_sync, "llm_tokens", provider_credential, amount)


async def reserve_search_request(provider_credential: str) -> None:
    await asyncio.to_thread(_reserve_sync, "search_requests", provider_credential, 1)


async def reserve_asr_millis(provider_credential: str, millis: int) -> None:
    """按毫秒预留 ASR 预算。

    上限按秒配置、按毫秒入桶，所以 400 ms 的分片就记 400，不再被向上取整成
    一整秒。`max(1, ...)` 只是挡住 0 和负数（`reserve_usage` 要求 amount > 0），
    不再放大真实用量。
    """
    await asyncio.to_thread(
        _reserve_sync, "asr_millis", provider_credential, max(1, millis)
    )


def _gate(service: str) -> asyncio.Semaphore:
    try:
        env_name, default = _CONCURRENCY_ENV[service]
    except KeyError as exc:
        raise ValueError("未知付费服务") from exc
    loop = asyncio.get_running_loop()
    limit = _positive_int(env_name, default)
    with _gates_lock:
        current = _gates.get(service)
        if current is None or current[0] is not loop or current[1] != limit:
            current = (loop, limit, asyncio.Semaphore(limit))
            _gates[service] = current
    return current[2]


@asynccontextmanager
async def paid_call_slot(service: str):
    semaphore = _gate(service)
    try:
        timeout = float(os.environ.get("AI_PAID_CALL_QUEUE_TIMEOUT_SECONDS", "1.0"))
    except ValueError as exc:
        raise RuntimeError("AI_PAID_CALL_QUEUE_TIMEOUT_SECONDS 必须是数字") from exc
    if timeout <= 0:
        raise RuntimeError("AI_PAID_CALL_QUEUE_TIMEOUT_SECONDS 必须大于 0")
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise PaidCallBusyError(service) from exc
    try:
        yield
    finally:
        semaphore.release()


def reset_runtime_state() -> None:
    """测试和应用生命周期切换时清除仅存在于进程内的并发门。"""
    with _gates_lock:
        _gates.clear()
