from __future__ import annotations

import asyncio

import pytest

from app import cost_control, db


@pytest.mark.asyncio
async def test_usage_reservation_is_persistent_across_runtime_reset(monkeypatch):
    monkeypatch.setenv("AI_LLM_TOKENS_PER_MINUTE", "10")
    monkeypatch.setenv("AI_LLM_TOKENS_PER_HOUR", "20")
    monkeypatch.setenv("AI_LLM_TOKENS_PER_DAY", "30")

    await cost_control.reserve_llm_tokens("provider-key", 6)
    cost_control.reset_runtime_state()
    with pytest.raises(db.UsageLimitExceeded):
        await cost_control.reserve_llm_tokens("provider-key", 5)


@pytest.mark.asyncio
async def test_global_concurrency_gate_rejects_excess_work(monkeypatch):
    monkeypatch.setenv("AI_LLM_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("AI_PAID_CALL_QUEUE_TIMEOUT_SECONDS", "0.01")
    cost_control.reset_runtime_state()

    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_slot():
        async with cost_control.paid_call_slot("llm"):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_slot())
    await entered.wait()
    with pytest.raises(cost_control.PaidCallBusyError):
        async with cost_control.paid_call_slot("llm"):
            pass
    release.set()
    await holder
