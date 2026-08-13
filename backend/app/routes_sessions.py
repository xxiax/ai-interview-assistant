"""会话管理 API 路由。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    title: str


@router.post("")
async def create_session(req: CreateSessionRequest):
    conn = db.get_db()
    try:
        session = db.create_session(conn, req.title)
    finally:
        conn.close()
    return session


@router.get("")
async def list_sessions():
    conn = db.get_db()
    try:
        rows = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/{session_id}")
async def get_session(session_id: str):
    conn = db.get_db()
    try:
        session = db.get_session(conn, session_id)
    finally:
        conn.close()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.post("/{session_id}/end")
async def end_session(session_id: str):
    conn = db.get_db()
    try:
        session = db.get_session(conn, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        db.end_session(conn, session_id)
        return db.get_session(conn, session_id)
    finally:
        conn.close()


@router.get("/{session_id}/transcripts")
async def get_transcripts(session_id: str):
    conn = db.get_db()
    try:
        return db.get_transcripts(conn, session_id)
    finally:
        conn.close()


@router.get("/{session_id}/answers")
async def get_answers(session_id: str):
    conn = db.get_db()
    try:
        return db.get_answers(conn, session_id)
    finally:
        conn.close()
