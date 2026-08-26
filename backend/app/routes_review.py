"""已结束会话的复盘生成与持久化 API。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated

import httpx2 as httpx
from fastapi import APIRouter, Body, Depends, HTTPException

from . import cost_control, db, llm, search
from .models import ReviewRequest, ReviewResponse
from .realtime import run_db
from .security import require_auth

logger = logging.getLogger(__name__)
_review_locks: dict[str, tuple[asyncio.Lock, int]] = {}
router = APIRouter(
    prefix="/api/sessions",
    tags=["review"],
    dependencies=[Depends(require_auth)],
)


def _session_error(exc: Exception) -> HTTPException:
    if isinstance(exc, db.SessionNotFoundError):
        return HTTPException(status_code=404, detail="会话不存在")
    if isinstance(exc, db.SessionStateError):
        return HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "current_status": exc.current_status,
                "expected": exc.expected,
            },
        )
    raise exc


def _usage_error(exc: Exception) -> HTTPException:
    if isinstance(exc, db.UsageLimitExceeded):
        return HTTPException(
            status_code=429,
            detail="付费服务用量预算已耗尽，请稍后重试",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    if isinstance(exc, cost_control.PaidCallBusyError):
        return HTTPException(
            status_code=429,
            detail="付费服务当前繁忙，请稍后重试",
            headers={"Retry-After": "1"},
        )
    raise exc


def _review_request_key(
    transcripts: list[dict], answers: list[dict], use_search: bool
) -> str:
    material = {
        "version": 1,
        "use_search": use_search,
        "transcripts": transcripts,
        "answers": answers,
    }
    serialized = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@asynccontextmanager
async def _review_lock(request_key: str):
    entry = _review_locks.get(request_key)
    lock, users = entry if entry is not None else (asyncio.Lock(), 0)
    _review_locks[request_key] = (lock, users + 1)
    try:
        async with lock:
            yield
    finally:
        current = _review_locks.get(request_key)
        if current is not None and current[0] is lock:
            remaining = current[1] - 1
            if remaining == 0:
                _review_locks.pop(request_key, None)
            else:
                _review_locks[request_key] = (lock, remaining)


@router.post("/{session_id}/review", response_model=ReviewResponse)
async def generate_review(
    session_id: str,
    req: Annotated[ReviewRequest | None, Body()] = None,
):
    req = req or ReviewRequest()
    try:
        session = await run_db(db.require_session, session_id)
        if session["status"] != "ended":
            raise db.SessionStateError(session["status"], "ended")
        transcripts = await run_db(db.get_transcripts, session_id)
        answers = await run_db(db.get_answers, session_id)
    except Exception as exc:
        raise _session_error(exc) from exc

    if not transcripts:
        raise HTTPException(status_code=400, detail="会话没有转写记录")

    request_key = _review_request_key(transcripts, answers, req.use_search)
    cached = await run_db(db.get_review_by_request_key, session_id, request_key)
    if cached:
        return cached

    async with _review_lock(request_key):
        cached = await run_db(db.get_review_by_request_key, session_id, request_key)
        if cached:
            return cached

        search_results: list[dict] = []
        if req.use_search:
            questions = [
                answer["question"] for answer in answers if answer.get("question")
            ]
            query = " ".join(questions).strip()
            if not query:
                query = " ".join(item["text"] for item in transcripts[-10:]).strip()
            query = query[:1000]
            if query:
                try:
                    search_results = await search.search_web(query)
                except (db.UsageLimitExceeded, cost_control.PaidCallBusyError) as exc:
                    raise _usage_error(exc) from exc
                except Exception:
                    logger.exception("复盘搜索增强失败，降级为纯 LLM")
                    search_results = []

        try:
            content = await llm.generate_review(
                transcripts, answers, search_results or None
            )
        except (db.UsageLimitExceeded, cost_control.PaidCallBusyError) as exc:
            raise _usage_error(exc) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="LLM 上游请求失败") from exc
        except llm.LLMInputTooLongError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503, detail="LLM 服务未配置或返回异常"
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="复盘生成失败") from exc

        source = "search+llm" if search_results else "llm"
        try:
            return await run_db(db.add_review, session_id, content, source, request_key)
        except Exception as exc:
            raise _session_error(exc) from exc


@router.get("/{session_id}/reviews", response_model=list[ReviewResponse])
async def get_reviews(session_id: str):
    try:
        return await run_db(db.get_reviews, session_id)
    except Exception as exc:
        raise _session_error(exc) from exc
