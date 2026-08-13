"""WebSocket 实时转写与答案生成。"""
import base64
import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from . import db, asr, llm


class ConnectionManager:
    """管理会话的 WebSocket 连接。"""

    def __init__(self):
        self.connections: dict[str, list[WebSocket]] = {}

    async def connect(self, session_id: str, ws: WebSocket):
        await ws.accept()
        if session_id not in self.connections:
            self.connections[session_id] = []
        self.connections[session_id].append(ws)

    def disconnect(self, session_id: str, ws: WebSocket):
        if session_id in self.connections:
            self.connections[session_id].remove(ws)
            if not self.connections[session_id]:
                del self.connections[session_id]

    async def broadcast(self, session_id: str, message: dict):
        """向会话的所有连接广播消息。"""
        for ws in self.connections.get(session_id, []):
            try:
                await ws.send_json(message)
            except Exception:
                pass


manager = ConnectionManager()


async def _handle_audio_chunk(session_id: str, source: str, audio_b64: str):
    """处理音频分片：转写 + 生成答案。"""
    try:
        if not audio_b64:
            return
        audio_bytes = base64.b64decode(audio_b64)
        try:
            text = await asr.transcribe_audio(audio_bytes, source)
        except Exception as e:
            await manager.broadcast(session_id, {"type": "error", "message": f"转写失败: {e}"})
            return
        if not text.strip():
            return

        conn = db.get_db()
        try:
            transcript = db.add_transcript(conn, session_id, source, text.strip())
        finally:
            conn.close()
        await manager.broadcast(session_id, {
            "type": "transcript",
            "session_id": session_id,
            "source": source,
            "text": text.strip(),
            "seq": transcript["seq"],
        })

        # 若文本像问题，生成答案
        if _looks_like_question(text):
            try:
                answer = await llm.generate_answer(text.strip())
            except Exception as e:
                await manager.broadcast(session_id, {"type": "error", "message": f"答案生成失败: {e}"})
                return
            conn = db.get_db()
            try:
                db.add_answer(conn, session_id, text.strip(), answer, "llm")
            finally:
                conn.close()
            await manager.broadcast(session_id, {
                "type": "answer",
                "session_id": session_id,
                "question": text.strip(),
                "answer": answer,
            })
    except Exception as e:
        # 兜底：避免 asyncio.create_task 中的异常变成 "Task exception was never retrieved"
        await manager.broadcast(session_id, {"type": "error", "message": f"处理音频分片失败: {e}"})


def _looks_like_question(text: str) -> bool:
    """判断文本是否像面试问题。"""
    question_markers = ["?", "？", "吗", "呢", "如何", "怎么", "为什么", "什么", "哪些", "请", "介绍", "说说", "谈谈"]
    return any(m in text for m in question_markers)


async def websocket_endpoint(ws: WebSocket, session_id: str):
    """WebSocket 端点：处理音频分片和会话控制。"""
    conn = db.get_db()
    try:
        session = db.get_session(conn, session_id)
    finally:
        conn.close()
    if not session:
        await ws.close(code=4004, reason="会话不存在")
        return

    await manager.connect(session_id, ws)
    # 广播当前会话状态
    await manager.broadcast(session_id, {
        "type": "session_state",
        "session_id": session_id,
        "status": session["status"],
        "radio_mode": session["radio_mode"],
    })

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            if msg_type == "audio_chunk":
                source = data.get("source", "pc")
                audio_b64 = data.get("data", "")
                asyncio.create_task(_handle_audio_chunk(session_id, source, audio_b64))

            elif msg_type == "set_radio_mode":
                mode = data.get("mode", "pc")
                conn = db.get_db()
                try:
                    conn.execute("UPDATE sessions SET radio_mode = ? WHERE id = ?", (mode, session_id))
                    conn.commit()
                finally:
                    conn.close()
                await manager.broadcast(session_id, {
                    "type": "session_state",
                    "session_id": session_id,
                    "status": "recording",
                    "radio_mode": mode,
                })

            elif msg_type == "regenerate_answer":
                question = data.get("question", "")
                if question:
                    try:
                        answer = await llm.generate_answer(question)
                    except Exception as e:
                        await manager.broadcast(session_id, {"type": "error", "message": f"答案生成失败: {e}"})
                        continue
                    conn = db.get_db()
                    try:
                        db.add_answer(conn, session_id, question, answer, "llm")
                    finally:
                        conn.close()
                    await manager.broadcast(session_id, {
                        "type": "answer",
                        "session_id": session_id,
                        "question": question,
                        "answer": answer,
                    })

            elif msg_type == "end_session":
                conn = db.get_db()
                try:
                    db.end_session(conn, session_id)
                finally:
                    conn.close()
                await manager.broadcast(session_id, {
                    "type": "session_state",
                    "session_id": session_id,
                    "status": "ended",
                    "radio_mode": session["radio_mode"],
                })
                break

    except WebSocketDisconnect:
        pass
    finally:
        # 无论正常退出（end_session）、异常（JSONDecodeError 等）还是断开，都清理连接
        manager.disconnect(session_id, ws)
