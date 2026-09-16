"""会话管理 REST API。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from . import db
from .models import (
    AnswerResponse,
    AudioChunkResponse,
    AudioSource,
    CreateSessionRequest,
    EventResponse,
    SessionResponse,
    StartSessionRequest,
    TranscriptResponse,
    UpdateSessionContextRequest,
)
from .protocol import event_message
from .realtime import run_db
from .security import require_auth
from .ws import manager, pipeline

router = APIRouter(
    prefix="/api/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_auth)],
)


def _translate_db_error(exc: Exception) -> HTTPException:
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
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(req: CreateSessionRequest):
    try:
        return await run_db(db.create_session, req.title)
    except Exception as exc:
        raise _translate_db_error(exc) from exc


@router.get("", response_model=list[SessionResponse])
async def list_sessions(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=db.SQLITE_INT_MAX)] = 0,
):
    return await run_db(db.list_sessions, limit, offset)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str):
    try:
        return await run_db(db.require_session, session_id)
    except Exception as exc:
        raise _translate_db_error(exc) from exc


@router.put("/{session_id}/context", response_model=SessionResponse)
async def update_session_context(session_id: str, req: UpdateSessionContextRequest):
    """写入会话级岗位 JD 与简历。任何会话状态都允许，不广播事件。"""
    try:
        return await run_db(
            db.set_session_context, session_id, req.job_description, req.resume
        )
    except Exception as exc:
        raise _translate_db_error(exc) from exc


@router.post("/{session_id}/start", response_model=SessionResponse)
async def start_session(session_id: str, req: StartSessionRequest):
    try:
        session, event = await run_db(db.start_session, session_id, req.radio_mode)
    except Exception as exc:
        raise _translate_db_error(exc) from exc
    await manager.broadcast(session_id, event_message(event))
    return session


@router.post("/{session_id}/end", response_model=SessionResponse)
async def end_session(session_id: str):
    await pipeline.flush_session(session_id)
    try:
        session, event = await run_db(db.end_session, session_id)
    except Exception as exc:
        raise _translate_db_error(exc) from exc

    await pipeline.stop_session(session_id)
    if event:
        await manager.broadcast(session_id, event_message(event))
    await manager.close_session(session_id)
    return session


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str):
    try:
        await run_db(db.delete_session, session_id)
    except db.SessionNotFoundError:
        raise _translate_db_error(db.SessionNotFoundError(session_id)) from None
    except Exception as exc:
        raise _translate_db_error(exc) from exc
    # 停掉在途处理并断开仍连接的 WebSocket
    await pipeline.stop_session(session_id)
    await manager.close_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{session_id}/transcripts", response_model=list[TranscriptResponse])
async def get_transcripts(session_id: str):
    try:
        return await run_db(db.get_transcripts, session_id)
    except Exception as exc:
        raise _translate_db_error(exc) from exc


@router.get("/{session_id}/answers", response_model=list[AnswerResponse])
async def get_answers(session_id: str):
    try:
        return await run_db(db.get_answers, session_id)
    except Exception as exc:
        raise _translate_db_error(exc) from exc


@router.get("/{session_id}/audio-chunks", response_model=list[AudioChunkResponse])
async def get_audio_chunks(
    session_id: str,
    source: AudioSource | None = None,
    after_source: AudioSource | None = None,
    after_chunk_seq: Annotated[int, Query(ge=-1, le=db.CHUNK_SEQ_MAX)] = -1,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
):
    try:
        return await run_db(
            db.get_audio_chunks,
            session_id,
            source=source,
            after_source=after_source,
            after_chunk_seq=after_chunk_seq,
            limit=limit,
        )
    except Exception as exc:
        raise _translate_db_error(exc) from exc


@router.get("/{session_id}/events", response_model=list[EventResponse])
async def get_events(
    session_id: str,
    after_event_id: Annotated[int, Query(ge=0, le=db.SQLITE_INT_MAX)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
):
    try:
        return await run_db(db.get_events, session_id, after_event_id, limit)
    except Exception as exc:
        raise _translate_db_error(exc) from exc
