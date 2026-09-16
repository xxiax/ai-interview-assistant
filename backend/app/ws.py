"""认证且可恢复的 WebSocket 实时协议。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import WebSocket, WebSocketDisconnect

from . import db
from .protocol import (
    AudioChunkMessage,
    CancelAudioSourceMessage,
    EndSessionMessage,
    PingMessage,
    RegenerateAnswerMessage,
    ResumeMessage,
    SetRadioModeMessage,
    SolveScreenshotMessage,
    SpeechEndMessage,
    StartSessionMessage,
    event_message,
    parse_auth_message,
    parse_client_message,
    server_message,
)
from .realtime import AnswerWork, AudioWork, RealtimePipeline, run_db
from .security import rate_limiter, token_fingerprint, verify_token

logger = logging.getLogger(__name__)
AUTH_TIMEOUT_SECONDS = 5
SEND_TIMEOUT_SECONDS = 3
EventLoader = Callable[[str, int, int], Awaitable[list[dict]]]


class ConnectionManager:
    """管理单 worker 内同一会话的所有连接。"""

    def __init__(self, event_loader: EventLoader | None = None) -> None:
        self.connections: dict[str, set[WebSocket]] = {}
        self.send_locks: dict[WebSocket, asyncio.Lock] = {}
        self.event_watermarks: dict[WebSocket, int] = {}
        self._session_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._event_loader = event_loader

    @asynccontextmanager
    async def session_lock(self, session_id: str):
        entry = self._session_locks.get(session_id)
        lock, users = entry if entry is not None else (asyncio.Lock(), 0)
        self._session_locks[session_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            current = self._session_locks.get(session_id)
            if current is not None and current[0] is lock:
                remaining = current[1] - 1
                if remaining == 0:
                    self._session_locks.pop(session_id, None)
                else:
                    self._session_locks[session_id] = (lock, remaining)

    def connect(self, session_id: str, ws: WebSocket, last_event_id: int = 0) -> None:
        self.connections.setdefault(session_id, set()).add(ws)
        self.send_locks.setdefault(ws, asyncio.Lock())
        self.event_watermarks[ws] = max(0, last_event_id)

    def disconnect(self, session_id: str, ws: WebSocket) -> None:
        self.send_locks.pop(ws, None)
        self.event_watermarks.pop(ws, None)
        sockets = self.connections.get(session_id)
        if not sockets:
            return
        sockets.discard(ws)
        if not sockets:
            self.connections.pop(session_id, None)

    async def send(self, ws: WebSocket, message: dict) -> bool:
        try:
            lock = self.send_locks.setdefault(ws, asyncio.Lock())
            async with lock:
                event_id = message.get("event_id")
                if type(event_id) is int and event_id <= self.event_watermarks.get(
                    ws, 0
                ):
                    return True
                await asyncio.wait_for(
                    ws.send_json(message), timeout=SEND_TIMEOUT_SECONDS
                )
                if type(event_id) is int:
                    self.event_watermarks[ws] = event_id
            return True
        except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError):
            return False

    async def replay(
        self,
        ws: WebSocket,
        session_id: str,
        through_event_id: int,
    ) -> bool:
        if self._event_loader is None:
            raise RuntimeError("未配置事件重放加载器")
        latest_sent = self.event_watermarks.get(ws, 0)
        while latest_sent < through_event_id:
            events = await self._event_loader(session_id, latest_sent, 200)
            events = [
                event for event in events if event["event_id"] <= through_event_id
            ]
            if not events:
                return True
            for event in events:
                if not await self.send(ws, event_message(event)):
                    return False
                latest_sent = event["event_id"]
        return True

    async def _send_in_event_order(
        self, ws: WebSocket, session_id: str, message: dict
    ) -> bool:
        event_id = message.get("event_id")
        if type(event_id) is not int or self._event_loader is None:
            return await self.send(ws, message)
        if event_id <= self.event_watermarks.get(ws, 0):
            return True
        return await self.replay(ws, session_id, event_id)

    async def broadcast(self, session_id: str, message: dict) -> None:
        async with self.session_lock(session_id):
            sockets = list(self.connections.get(session_id, set()))
            if not sockets:
                return
            results = await asyncio.gather(
                *(self._send_in_event_order(ws, session_id, message) for ws in sockets)
            )
            for ws, sent in zip(sockets, results):
                if not sent:
                    self.disconnect(session_id, ws)

    async def close_session(self, session_id: str, code: int = 1000) -> None:
        async with self.session_lock(session_id):
            sockets = list(self.connections.get(session_id, set()))
            self.connections.pop(session_id, None)
            await asyncio.gather(
                *(ws.close(code=code) for ws in sockets), return_exceptions=True
            )
            for ws in sockets:
                self.send_locks.pop(ws, None)
                self.event_watermarks.pop(ws, None)

    async def shutdown(self) -> None:
        sockets = [
            ws
            for session_sockets in self.connections.values()
            for ws in session_sockets
        ]
        self.connections.clear()
        await asyncio.gather(
            *(ws.close(code=1001) for ws in sockets), return_exceptions=True
        )
        self.send_locks.clear()
        self.event_watermarks.clear()
        self._session_locks.clear()


async def _load_events(session_id: str, after_event_id: int, limit: int) -> list[dict]:
    return await run_db(db.get_events, session_id, after_event_id, limit)


manager = ConnectionManager(event_loader=_load_events)
pipeline = RealtimePipeline(manager.broadcast)


async def _send_error(ws: WebSocket, code: str, message: str, **details) -> None:
    await manager.send(
        ws, server_message("error", code=code, message=message, **details)
    )


def _decode_audio(data: str) -> bytes:
    max_bytes = int(os.environ.get("AI_MAX_AUDIO_CHUNK_BYTES", str(2 * 1024 * 1024)))
    if len(data) > ((max_bytes + 2) // 3) * 4 + 4:
        raise ValueError("音频分片过大")
    try:
        audio_bytes = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("音频数据不是有效 Base64") from exc
    if not audio_bytes or len(audio_bytes) > max_bytes:
        raise ValueError("音频分片大小非法")
    return audio_bytes


def _decode_screenshot(data: str) -> bytes:
    """解码笔试截图，并按容器魔数确认它真是 PNG/JPEG。

    上限默认 6 MiB：1080p PNG 截图通常 1-3 MiB，留两倍余量。校验魔数是因为
    mime 字段来自客户端，不能让一个声明 image/png 的任意二进制流进 LLM。
    """
    max_bytes = int(os.environ.get("AI_MAX_SCREENSHOT_BYTES", str(6 * 1024 * 1024)))
    if len(data) > ((max_bytes + 2) // 3) * 4 + 4:
        raise ValueError("截图过大")
    try:
        image_bytes = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("截图不是有效 Base64") from exc
    if not image_bytes or len(image_bytes) > max_bytes:
        raise ValueError("截图大小非法")
    if not image_bytes.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        raise ValueError("截图必须是 PNG 或 JPEG")
    return image_bytes


def _source_allowed(session: dict, source: str) -> bool:
    return session["radio_mode"] == "both" or session["radio_mode"] == source


def _origin_allowed(ws: WebSocket) -> bool:
    allowed = {
        item.strip()
        for item in os.environ.get("AI_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    }
    origin = ws.headers.get("origin")
    if not origin:
        return True
    return origin in allowed


async def websocket_endpoint(ws: WebSocket, session_id: str) -> None:
    """WebSocket v1：先认证，再同步事件，然后处理严格消息模型。"""
    await ws.accept()
    authenticated = False
    try:
        if not _origin_allowed(ws):
            await _send_error(ws, "origin_not_allowed", "WebSocket Origin 不在允许列表")
            await ws.close(code=4403)
            return
        client_host = ws.client.host if ws.client else "unknown"
        if not rate_limiter.allow(f"ws-auth:{client_host}", 30, 60):
            await _send_error(ws, "rate_limited", "认证请求过于频繁")
            await ws.close(code=4429)
            return
        try:
            raw_auth = await asyncio.wait_for(
                ws.receive_json(), timeout=AUTH_TIMEOUT_SECONDS
            )
            auth = parse_auth_message(raw_auth)
        except (asyncio.TimeoutError, ValueError):
            await _send_error(
                ws, "authentication_required", "请先发送有效的 authenticate 消息"
            )
            await ws.close(code=4401)
            return

        if not verify_token(auth.token):
            await _send_error(ws, "authentication_failed", "认证失败")
            await ws.close(code=4401)
            return
        authenticated = True

        try:
            session = await run_db(db.require_session, session_id)
        except db.SessionNotFoundError:
            await _send_error(ws, "session_not_found", "会话不存在")
            await ws.close(code=4404)
            return

        async with manager.session_lock(session_id):
            session, latest_event_id = await run_db(db.get_session_snapshot, session_id)
            manager.connect(
                session_id, ws, last_event_id=min(auth.last_event_id, latest_event_id)
            )
            if not await manager.replay(ws, session_id, latest_event_id):
                raise WebSocketDisconnect(code=1006)
            if not await manager.send(
                ws,
                server_message(
                    "sync_complete",
                    session_id=session_id,
                    latest_event_id=latest_event_id,
                    status=session["status"],
                    radio_mode=session["radio_mode"],
                ),
            ):
                raise WebSocketDisconnect(code=1006)

        connection_key = f"ws:{session_id}:{id(ws)}"
        while True:
            try:
                raw_message = await ws.receive_json()
            except ValueError:
                await _send_error(ws, "invalid_message", "消息必须是有效 JSON")
                continue
            except RuntimeError as exc:
                if "WebSocket is not connected" in str(exc):
                    raise WebSocketDisconnect(code=1006) from exc
                raise
            if not rate_limiter.allow(connection_key, 600, 60):
                await _send_error(ws, "rate_limited", "消息过于频繁")
                continue
            try:
                message = parse_client_message(raw_message)
            except ValueError as exc:
                await _send_error(ws, "invalid_message", str(exc))
                continue

            try:
                if isinstance(message, StartSessionMessage):
                    _, event = await run_db(
                        db.start_session, session_id, message.radio_mode
                    )
                    await manager.broadcast(session_id, event_message(event))

                elif isinstance(message, SetRadioModeMessage):
                    _, event = await run_db(db.set_radio_mode, session_id, message.mode)
                    if message.mode == "mobile":
                        max_pc_seq = await run_db(
                            db.get_max_audio_chunk_seq, session_id, "pc"
                        )
                        if max_pc_seq >= 0:
                            await pipeline.cancel_audio_source(
                                session_id,
                                "pc",
                                max_pc_seq,
                                "source_disabled",
                            )
                    await manager.broadcast(session_id, event_message(event))

                elif isinstance(message, CancelAudioSourceMessage):
                    await pipeline.cancel_audio_source(
                        session_id,
                        message.source,
                        message.through_chunk_seq,
                        message.reason,
                    )

                elif isinstance(message, SpeechEndMessage):
                    session = await run_db(db.ensure_recording, session_id)
                    if not _source_allowed(session, message.source):
                        await _send_error(
                            ws,
                            "source_not_allowed",
                            "当前收音模式不允许该音频来源",
                        )
                        continue
                    await pipeline.mark_speech_end(
                        session_id,
                        message.source,
                        message.through_chunk_seq,
                    )

                elif isinstance(message, AudioChunkMessage):
                    session = await run_db(db.ensure_recording, session_id)
                    if not _source_allowed(session, message.source):
                        await _send_error(
                            ws,
                            "source_not_allowed",
                            "当前收音模式不允许该音频来源",
                            chunk_id=str(message.chunk_id),
                        )
                        continue
                    try:
                        audio_bytes = _decode_audio(message.data)
                    except ValueError as exc:
                        await _send_error(
                            ws,
                            "invalid_audio",
                            str(exc),
                            chunk_id=str(message.chunk_id),
                        )
                        continue
                    accepted, record = await pipeline.enqueue_audio(
                        AudioWork(
                            session_id=session_id,
                            chunk_id=str(message.chunk_id),
                            source=message.source,
                            codec=message.codec,
                            chunk_seq=message.chunk_seq,
                            captured_at=message.captured_at,
                            duration_ms=message.duration_ms,
                            audio_bytes=audio_bytes,
                        )
                    )
                    if accepted:
                        await manager.broadcast(
                            session_id,
                            server_message(
                                "chunk_ack",
                                event_id=record.get("event_id"),
                                session_id=session_id,
                                chunk_id=str(message.chunk_id),
                                chunk_seq=message.chunk_seq,
                                status=record.get("status", "queued"),
                                error_code=record.get("error_code"),
                            ),
                        )
                    elif record.get("status") == "backpressure":
                        await _send_error(
                            ws,
                            "audio_backpressure",
                            "音频处理队列已满，请稍后重试",
                            chunk_id=str(message.chunk_id),
                        )
                    else:
                        await manager.send(
                            ws,
                            server_message(
                                "chunk_ack",
                                chunk_id=str(message.chunk_id),
                                chunk_seq=message.chunk_seq,
                                status=record.get("status", "duplicate"),
                                duplicate=True,
                            ),
                        )

                elif isinstance(message, RegenerateAnswerMessage):
                    await run_db(db.ensure_recording, session_id)
                    regenerate_key = f"regenerate:{token_fingerprint(auth.token)}"
                    if not rate_limiter.allow(regenerate_key, 10, 60):
                        await _send_error(ws, "rate_limited", "重新生成请求过于频繁")
                        continue
                    if message.thread_id is not None:
                        # 带线程 id：重问某张问题卡。revision+1、答案流回
                        # 同一张卡，完成后按线程规则落库。
                        accepted = await pipeline.regenerate_thread_answer(
                            session_id,
                            message.thread_id,
                            message.question,
                            message.use_search,
                        )
                    else:
                        # 不带:手动提问式重新生成,独立成卡立即入库。
                        accepted = await pipeline.enqueue_answer(
                            AnswerWork(session_id, message.question, message.use_search)
                        )
                    if not accepted:
                        await _send_error(
                            ws, "answer_backpressure", "答案生成队列已满，请稍后重试"
                        )

                elif isinstance(message, SolveScreenshotMessage):
                    await run_db(db.ensure_recording, session_id)
                    # 和 regenerate 共用一条限流键：两者都是用户手点触发的
                    # 付费调用，合起来 10 次/分钟才是真实的花钱速率上限。
                    solve_key = f"regenerate:{token_fingerprint(auth.token)}"
                    if not rate_limiter.allow(solve_key, 10, 60):
                        await _send_error(ws, "rate_limited", "解题请求过于频繁")
                        continue
                    try:
                        image_bytes = _decode_screenshot(message.image)
                    except ValueError as exc:
                        await _send_error(ws, "invalid_screenshot", str(exc))
                        continue
                    if not await pipeline.enqueue_answer(
                        AnswerWork(
                            session_id=session_id,
                            # 截图题没有转写出来的问题文本，用备注或固定标题
                            # 占位；answers.question 非空是 db 层的硬约束。
                            question=(message.note.strip() or "截图题目"),
                            use_search=False,
                            image_bytes=image_bytes,
                            image_mime=message.mime,
                        )
                    ):
                        await _send_error(
                            ws, "answer_backpressure", "解题队列已满，请稍后重试"
                        )

                elif isinstance(message, ResumeMessage):
                    async with manager.session_lock(session_id):
                        session, latest_event_id = await run_db(
                            db.get_session_snapshot, session_id
                        )
                        self_cursor = min(message.after_event_id, latest_event_id)
                        manager.event_watermarks[ws] = max(
                            manager.event_watermarks.get(ws, 0), self_cursor
                        )
                        if not await manager.replay(ws, session_id, latest_event_id):
                            raise WebSocketDisconnect(code=1006)
                        if not await manager.send(
                            ws,
                            server_message(
                                "sync_complete",
                                session_id=session_id,
                                latest_event_id=latest_event_id,
                                status=session["status"],
                                radio_mode=session["radio_mode"],
                            ),
                        ):
                            raise WebSocketDisconnect(code=1006)

                elif isinstance(message, PingMessage):
                    await manager.send(ws, server_message("pong"))

                elif isinstance(message, EndSessionMessage):
                    await pipeline.flush_session(session_id)
                    _, event = await run_db(db.end_session, session_id)
                    await pipeline.stop_session(session_id)
                    if event:
                        await manager.broadcast(session_id, event_message(event))
                    await manager.close_session(session_id)
                    return

            except db.SessionNotFoundError:
                await _send_error(ws, "session_not_found", "会话不存在")
            except db.SessionStateError as exc:
                details = {
                    "current_status": exc.current_status,
                    "expected": exc.expected,
                }
                if isinstance(message, AudioChunkMessage):
                    details["chunk_id"] = str(message.chunk_id)
                await _send_error(
                    ws,
                    "invalid_session_state",
                    str(exc),
                    **details,
                )
            except db.AudioSourceNotAllowedError as exc:
                details = {}
                if isinstance(message, AudioChunkMessage):
                    details["chunk_id"] = str(message.chunk_id)
                await _send_error(
                    ws,
                    "source_not_allowed",
                    str(exc),
                    **details,
                )
            except ValueError as exc:
                details = {}
                if isinstance(message, AudioChunkMessage):
                    details["chunk_id"] = str(message.chunk_id)
                await _send_error(ws, "invalid_audio_chunk", str(exc), **details)
            except Exception:
                logger.exception("WebSocket 消息处理失败: session=%s", session_id)
                await _send_error(ws, "internal_error", "消息处理失败")

    except WebSocketDisconnect:
        pass
    finally:
        if authenticated:
            manager.disconnect(session_id, ws)
        else:
            manager.send_locks.pop(ws, None)
        rate_limiter.clear_prefix(f"ws:{session_id}:{id(ws)}")


async def shutdown_realtime() -> None:
    await pipeline.shutdown()
    await manager.shutdown()


def prepare_realtime() -> None:
    pipeline.prepare_for_current_loop()
