"""复盘报告 API 路由。"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from . import db, llm

router = APIRouter(prefix="/api/sessions", tags=["review"])


@router.post("/{session_id}/review")
async def generate_review(session_id: str):
    conn = db.get_db()
    try:
        session = db.get_session(conn, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        transcripts = db.get_transcripts(conn, session_id)
        answers = db.get_answers(conn, session_id)
    finally:
        conn.close()

    # 先校验 LLM 配置，未配置时返回明确错误（500），避免后续真实调用失败
    try:
        llm.ensure_llm_configured()
    except RuntimeError as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    if not transcripts:
        return JSONResponse(status_code=400, content={"detail": "会话没有转写记录"})

    try:
        review = await llm.generate_review(transcripts, answers)
    except RuntimeError as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"复盘生成失败: {e}"})

    return {"session_id": session_id, "review": review}
