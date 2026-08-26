from __future__ import annotations

import asyncio
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app import asr, db
from app import realtime as realtime_module
from app.realtime import (
    AnswerWork,
    AudioWork,
    RealtimePipeline,
    join_transcript_text,
    normalize_segment_text,
)


def _audio_work(session_id: str, chunk_seq: int, audio_bytes: bytes) -> AudioWork:
    return AudioWork(
        session_id=session_id,
        chunk_id=str(uuid4()),
        source="pc",
        codec="webm_opus",
        chunk_seq=chunk_seq,
        captured_at=datetime.now(timezone.utc),
        duration_ms=1000,
        audio_bytes=audio_bytes,
    )


async def _wait_for_chunk_status(
    session_id: str, chunk_seq: int, expected_status: str
) -> None:
    for _ in range(200):
        conn = db.get_db()
        try:
            row = conn.execute(
                """
                SELECT status FROM audio_chunks
                WHERE session_id = ? AND source = 'pc' AND chunk_seq = ?
                """,
                (session_id, chunk_seq),
            ).fetchone()
        finally:
            conn.close()
        if row and row["status"] == expected_status:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"chunk {chunk_seq} 未进入 {expected_status} 状态")


# ---------- 片间文本归一化(断句优化) ----------


def test_normalize_segment_text_overlapping_prefix_is_deduplicated():
    """新片段与前文尾部有 ≥4 字符重叠前缀时去掉重叠(取最长)。"""
    assert normalize_segment_text("…请介绍一下 FastAPI", "FastAPI 的事件机制") == "的事件机制"
    # 3 字符重叠("分布式")低于阈值不去重
    assert normalize_segment_text("我们来说一下分布式", "分布式事务一致性") == "分布式事务一致性"
    # 5 字符重叠("分布式事务")达到阈值，去重
    assert normalize_segment_text("我们说一下分布式事务", "分布式事务一致性") == "一致性"
    # 全部重叠 → 归一化后为空串(该片段是纯重复)
    assert normalize_segment_text("请介绍一下", "请介绍一下") == ""


def test_normalize_segment_text_short_overlap_below_threshold_kept():
    """重叠少于 4 个字符时不去重，避免误删短英文词。"""
    assert normalize_segment_text("how do you", "you handle it") == "you handle it"
    assert normalize_segment_text("说一说", "一说细节") == "一说细节"


def test_normalize_segment_text_joins_cjk_without_space_and_english_with_space():
    prev = "请解释一下什么是一致性哈希"
    normalized = normalize_segment_text(prev, "一致性哈希的扩容原理是什么？")
    assert join_transcript_text(prev, normalized) == "请解释一下什么是一致性哈希的扩容原理是什么？"

    prev_en = "what is event loop"
    new_en = normalize_segment_text(prev_en, "event loop in Node.js")
    assert new_en == "in Node.js"
    assert join_transcript_text(prev_en, new_en) == "what is event loop in Node.js"


def test_normalize_segment_text_sentence_end_then_new_sentence():
    """prev 已到句末标点：拼接视为新句，补一个空格分隔。"""
    prev = "这个项目用到了消息队列。"
    new = normalize_segment_text(prev, "为什么选择 Kafka？")
    assert new == "为什么选择 Kafka？"
    assert join_transcript_text(prev, new) == "这个项目用到了消息队列。 为什么选择 Kafka？"


def test_normalize_segment_text_empty_inputs():
    assert normalize_segment_text("", "") == ""
    assert normalize_segment_text("", "新片段") == "新片段"
    assert normalize_segment_text("前文尾部", "") == ""
    assert normalize_segment_text("  前文尾部  ", "  新片段  ") == "新片段"


@pytest.mark.asyncio
async def test_audio_worker_normalizes_transcript_before_persist(monkeypatch):
    """入库前用上一条尾部去重，重叠前缀不双写；尾部缓存随会话清理。"""
    conn = db.get_db()
    try:
        session = db.create_session(conn, "片间归一化")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    async def fake_transcribe(audio_bytes, *_args):
        return audio_bytes.decode()

    async def ignore_broadcast(_session_id, _message):
        return None

    monkeypatch.setattr(asr, "transcribe_audio", fake_transcribe)
    pipeline = RealtimePipeline(ignore_broadcast)
    captured_at = datetime(2026, 8, 21, tzinfo=timezone.utc)
    texts = ("请介绍一下你熟悉的设计模式", "设计模式的实际应用场景")
    for seq, text in enumerate(texts):
        accepted, _ = await pipeline.enqueue_audio(
            AudioWork(
                session_id=session["id"],
                chunk_id=str(uuid4()),
                source="pc",
                codec="webm_opus",
                chunk_seq=seq,
                captured_at=captured_at,
                duration_ms=1000,
                audio_bytes=text.encode(),
            )
        )
        assert accepted
        await _wait_for_chunk_status(session["id"], seq, "done")

    conn = db.get_db()
    try:
        transcripts = db.get_transcripts(conn, session["id"])
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    assert [item["text"] for item in transcripts] == [
        "请介绍一下你熟悉的设计模式",
        "的实际应用场景",
    ]
    key = (session["id"], "pc")
    await pipeline.stop_session(session["id"])
    assert key not in pipeline._transcript_tails


@pytest.mark.asyncio
async def test_stop_session_cancels_inflight_and_queued_chunks(monkeypatch):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "取消测试")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    async def slow_transcribe(*_args):
        await asyncio.sleep(10)
        return "不会完成"

    async def ignore_broadcast(_session_id, _message):
        return None

    monkeypatch.setattr(asr, "transcribe_audio", slow_transcribe)
    pipeline = RealtimePipeline(ignore_broadcast)
    now = datetime.now(timezone.utc)
    for seq in (0, 1):
        accepted, _ = await pipeline.enqueue_audio(
            AudioWork(
                session_id=session["id"],
                chunk_id=str(uuid4()),
                source="pc",
                codec="webm_opus",
                chunk_seq=seq,
                captured_at=now,
                duration_ms=1000,
                audio_bytes=b"audio",
            )
        )
        assert accepted
    await asyncio.sleep(0.02)

    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])
    assert session["id"] not in pipeline._stopped_sessions

    conn = db.get_db()
    try:
        statuses = [
            row[0]
            for row in conn.execute(
                "SELECT status FROM audio_chunks WHERE session_id = ? ORDER BY chunk_seq",
                (session["id"],),
            ).fetchall()
        ]
        assert db.get_transcripts(conn, session["id"]) == []
    finally:
        conn.close()
    assert statuses == ["cancelled", "cancelled"]


@pytest.mark.asyncio
async def test_graceful_shutdown_leaves_inflight_chunk_retryable(monkeypatch):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "优雅关闭")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    started = asyncio.Event()

    async def slow_transcribe(*_args):
        started.set()
        await asyncio.sleep(10)
        return "不会完成"

    async def ignore_broadcast(_session_id, _message):
        return None

    monkeypatch.setattr(asr, "transcribe_audio", slow_transcribe)
    pipeline = RealtimePipeline(ignore_broadcast)
    chunk_id = str(uuid4())
    captured_at = datetime.now(timezone.utc)
    accepted, _ = await pipeline.enqueue_audio(
        AudioWork(
            session_id=session["id"],
            chunk_id=chunk_id,
            source="pc",
            codec="webm_opus",
            chunk_seq=0,
            captured_at=captured_at,
            duration_ms=1000,
            audio_bytes=b"audio",
        )
    )
    assert accepted
    await asyncio.wait_for(started.wait(), timeout=1)
    await pipeline.shutdown()

    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT status, error_code FROM audio_chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        assert tuple(row) == ("failed", "service_shutdown")
        assert db.get_next_audio_chunk_seq(conn, session["id"], "pc") == 0
        retried, record = db.reserve_audio_chunk(
            conn,
            chunk_id=chunk_id,
            session_id=session["id"],
            source="pc",
            codec="webm_opus",
            chunk_seq=0,
            captured_at=captured_at.isoformat(),
            duration_ms=1000,
            content_sha256=hashlib.sha256(b"audio").hexdigest(),
        )
    finally:
        conn.close()
    assert retried is True
    assert record["status"] == "queued"


@pytest.mark.asyncio
async def test_restart_retry_skips_already_done_successor(monkeypatch):
    conn = db.get_db()
    captured_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
    zero_id = str(uuid4())
    one_id = str(uuid4())
    try:
        session = db.create_session(conn, "重启后跳过已完成后继")
        db.start_session(conn, session["id"], "pc")
        for chunk_id, chunk_seq, audio_bytes in (
            (zero_id, 0, b"retry-zero"),
            (one_id, 1, b"already-one"),
        ):
            db.reserve_audio_chunk(
                conn,
                chunk_id=chunk_id,
                session_id=session["id"],
                source="pc",
                codec="webm_opus",
                chunk_seq=chunk_seq,
                captured_at=captured_at.isoformat(),
                duration_ms=1000,
                content_sha256=hashlib.sha256(audio_bytes).hexdigest(),
            )
        db.mark_audio_chunk_status(
            conn, zero_id, "failed", error_code="processing_failed"
        )
        db.mark_audio_chunk_status(conn, one_id, "done")
        assert db.get_next_audio_chunk_seq(conn, session["id"], "pc") == 0
    finally:
        conn.close()

    transcribed = []

    async def fake_transcribe(audio_bytes, *_args):
        transcribed.append(audio_bytes.decode())
        return audio_bytes.decode()

    async def ignore_broadcast(_session_id, _message):
        return None

    monkeypatch.setattr(asr, "transcribe_audio", fake_transcribe)
    pipeline = RealtimePipeline(ignore_broadcast)
    accepted, _ = await pipeline.enqueue_audio(
        AudioWork(
            session_id=session["id"],
            chunk_id=zero_id,
            source="pc",
            codec="webm_opus",
            chunk_seq=0,
            captured_at=captured_at,
            duration_ms=1000,
            audio_bytes=b"retry-zero",
        )
    )
    assert accepted
    await _wait_for_chunk_status(session["id"], 0, "done")

    accepted, _ = await pipeline.enqueue_audio(
        AudioWork(
            session_id=session["id"],
            chunk_id=str(uuid4()),
            source="pc",
            codec="webm_opus",
            chunk_seq=2,
            captured_at=captured_at,
            duration_ms=1000,
            audio_bytes=b"new-two",
        )
    )
    assert accepted
    await _wait_for_chunk_status(session["id"], 2, "done")

    conn = db.get_db()
    try:
        chunks = db.get_audio_chunks(conn, session["id"])
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    assert [(chunk["chunk_seq"], chunk["status"]) for chunk in chunks] == [
        (0, "done"),
        (1, "done"),
        (2, "done"),
    ]
    assert transcribed == ["retry-zero", "new-two"]
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_gap_timeout_does_not_drop_chunk_arriving_during_error_broadcast(
    monkeypatch,
):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "缺口竞态")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    error_started = asyncio.Event()
    release_error = asyncio.Event()
    transcripts = []

    async def coordinated_broadcast(_session_id, message):
        if message["type"] == "error" and message.get("code") == "audio_sequence_gap":
            error_started.set()
            await release_error.wait()
        if message["type"] == "transcript":
            transcripts.append(message["chunk_seq"])

    async def fake_transcribe(audio_bytes, *_args):
        return audio_bytes.decode()

    monkeypatch.setenv("AI_AUDIO_REORDER_WAIT_SECONDS", "0.01")
    monkeypatch.setattr(asr, "transcribe_audio", fake_transcribe)
    pipeline = RealtimePipeline(coordinated_broadcast)
    now = datetime.now(timezone.utc)
    later_chunk_id = str(uuid4())

    accepted, _ = await pipeline.enqueue_audio(
        AudioWork(
            session_id=session["id"],
            chunk_id=later_chunk_id,
            source="pc",
            codec="webm_opus",
            chunk_seq=1,
            captured_at=now,
            duration_ms=1000,
            audio_bytes=b"second",
        )
    )
    assert accepted
    await asyncio.wait_for(error_started.wait(), timeout=1)

    accepted, _ = await pipeline.enqueue_audio(
        AudioWork(
            session_id=session["id"],
            chunk_id=str(uuid4()),
            source="pc",
            codec="webm_opus",
            chunk_seq=0,
            captured_at=now,
            duration_ms=1000,
            audio_bytes=b"first",
        )
    )
    assert accepted
    release_error.set()

    for _ in range(100):
        if transcripts == [0]:
            break
        await asyncio.sleep(0.01)
    assert transcripts == [0]

    accepted, _ = await pipeline.enqueue_audio(
        AudioWork(
            session_id=session["id"],
            chunk_id=later_chunk_id,
            source="pc",
            codec="webm_opus",
            chunk_seq=1,
            captured_at=now,
            duration_ms=1000,
            audio_bytes=b"second",
        )
    )
    assert accepted
    for _ in range(100):
        if transcripts == [0, 1]:
            break
        await asyncio.sleep(0.01)
    assert transcripts == [0, 1]

    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_enqueue_cannot_create_worker_after_session_stop(monkeypatch):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "结束竞态")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    reserved = threading.Event()
    release_reservation = threading.Event()
    original_reserve = db.reserve_audio_chunk

    def delayed_reserve(conn, **kwargs):
        result = original_reserve(conn, **kwargs)
        reserved.set()
        release_reservation.wait(timeout=2)
        return result

    async def ignore_broadcast(_session_id, _message):
        return None

    monkeypatch.setattr(db, "reserve_audio_chunk", delayed_reserve)
    pipeline = RealtimePipeline(ignore_broadcast)
    enqueue_task = asyncio.create_task(
        pipeline.enqueue_audio(
            AudioWork(
                session_id=session["id"],
                chunk_id=str(uuid4()),
                source="pc",
                codec="webm_opus",
                chunk_seq=0,
                captured_at=datetime.now(timezone.utc),
                duration_ms=1000,
                audio_bytes=b"audio",
            )
        )
    )
    for _ in range(100):
        if reserved.is_set():
            break
        await asyncio.sleep(0.01)
    assert reserved.is_set()

    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])
    release_reservation.set()

    with pytest.raises(db.SessionStateError):
        await enqueue_task
    assert not pipeline._audio_tasks
    assert not pipeline._audio_queues
    assert session["id"] not in pipeline._stopped_sessions


@pytest.mark.asyncio
async def test_enqueue_answer_cannot_recreate_worker_after_session_stop(monkeypatch):
    """答案入队与 end_session 的 stop_session 交错时不得重建 answer worker。

    在 ensure_recording 的 await 期间插入 stop_session（用线程 Event 卡点，
    事件循环保持空闲）。断言 enqueue_answer 抛 SessionStateError、
    不重建 answer queue/worker、_stopped_sessions 最终释放。
    这是 enqueue_audio 三重屏障在答案路径上的对齐回归测试。
    """
    conn = db.get_db()
    try:
        session = db.create_session(conn, "答案结束竞态")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    passed_second_check = threading.Event()
    release_enqueue = threading.Event()
    original_run_db = realtime_module.run_db

    async def hooked_run_db(func, *args, **kwargs):
        result = await original_run_db(func, *args, **kwargs)
        if func is db.ensure_recording:
            # 卡点必须在线程中等待：若在协程里同步 wait 会冻住事件循环，
            # stop_session 永远没机会插进来
            passed_second_check.set()
            await asyncio.to_thread(release_enqueue.wait, 2)
        return result

    async def ignore_broadcast(_session_id, _message):
        return None

    monkeypatch.setattr(realtime_module, "run_db", hooked_run_db)
    pipeline = RealtimePipeline(ignore_broadcast)
    enqueue_task = asyncio.create_task(
        pipeline.enqueue_answer(AnswerWork(session["id"], "什么是 FastAPI？", False))
    )
    for _ in range(100):
        if passed_second_check.is_set():
            break
        await asyncio.sleep(0.01)
    assert passed_second_check.is_set()

    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])
    release_enqueue.set()

    with pytest.raises(db.SessionStateError):
        await enqueue_task
    # 不得为已停止的会话重建 answer worker/queue
    assert not pipeline._answer_tasks
    assert not pipeline._answer_queues
    assert session["id"] not in pipeline._stopped_sessions


@pytest.mark.asyncio
async def test_answer_stream_is_broadcast_before_final_answer_is_persisted(monkeypatch):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "流式答案")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    messages = []

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    async def fake_stream(question, context=""):
        assert question == "请介绍一下你的项目"
        yield realtime_module.llm.LLMStreamPart(thinking="先梳理项目背景")
        yield realtime_module.llm.LLMStreamPart(text="先给出项目背景")
        yield realtime_module.llm.LLMStreamPart(text="和技术方案。")

    async def unexpected_search(*_args, **_kwargs):
        raise AssertionError("use_search=false 时不应调用搜索增强链路")

    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)
    monkeypatch.setattr(
        realtime_module.llm, "stream_answer_with_search_info", unexpected_search
    )
    pipeline = RealtimePipeline(capture_broadcast)
    assert await pipeline.enqueue_answer(
        AnswerWork(session["id"], "请介绍一下你的项目", False)
    )

    for _ in range(100):
        if any(message.get("type") == "answer" for message in messages):
            break
        await asyncio.sleep(0.01)

    stream_messages = [
        message for message in messages if message.get("type") == "answer_stream"
    ]
    answer_stream_messages = [
        message for message in stream_messages if message["channel"] == "answer"
    ]
    assert [message["delta"] for message in answer_stream_messages if message["delta"]] == [
        "先给出项目背景",
        "和技术方案。",
    ]
    assert "".join(message["delta"] for message in answer_stream_messages if message["delta"]) == "先给出项目背景和技术方案。"
    thinking_stream_messages = [
        message for message in stream_messages if message["channel"] == "thinking"
    ]
    assert "".join(message["delta"] for message in thinking_stream_messages) == "先梳理项目背景"
    assert thinking_stream_messages[-1]["thinking"] == "先梳理项目背景"
    assert stream_messages[-1]["done"] is True
    final_answer_event = next(message for message in messages if message.get("type") == "answer")
    assert final_answer_event["thinking"] == "先梳理项目背景"
    conn = db.get_db()
    try:
        answers = db.get_answers(conn, session["id"])
    finally:
        conn.close()
    assert answers[0]["answer"] == "先给出项目背景和技术方案。"
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_answer_search_is_only_used_when_explicitly_requested(monkeypatch):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "显式搜索")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    calls = {"plain": 0, "search": 0}
    messages = []

    async def unexpected_plain(*_args, **_kwargs):
        calls["plain"] += 1
        raise AssertionError("use_search=true 时不应调用纯 LLM 链路")
        yield  # pragma: no cover

    async def fake_search(question, context=""):
        calls["search"] += 1
        assert question == "需要搜索的问题"
        yield realtime_module.llm.LLMStreamPart(text="搜索答案"), True

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    monkeypatch.setattr(realtime_module.llm, "stream_answer", unexpected_plain)
    monkeypatch.setattr(
        realtime_module.llm, "stream_answer_with_search_info", fake_search
    )
    pipeline = RealtimePipeline(capture_broadcast)
    assert await pipeline.enqueue_answer(
        AnswerWork(session["id"], "需要搜索的问题", True)
    )

    for _ in range(100):
        if any(message.get("type") == "answer" for message in messages):
            break
        await asyncio.sleep(0.01)

    answer = next(message for message in messages if message.get("type") == "answer")
    assert answer["source"] == "search+llm"
    assert calls == {"plain": 0, "search": 1}
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_funasr_partial_is_immediate_and_final_is_persisted_without_question_detection(
    monkeypatch,
):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "无问题检测实时流")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    monkeypatch.setenv("AI_FUNASR_STREAM", "true")
    messages = []

    class FakeStream:
        def __init__(self):
            self.push_count = 0

        async def connect(self):
            return None

        async def push_wav(self, _audio_bytes, _duration_ms):
            self.push_count += 1
            return [
                asr.FunAsrEvent("partial", text=f"还没说完{self.push_count}"),
                asr.FunAsrEvent("final", text=f"最终切片{self.push_count}"),
            ]

        async def finish(self):
            return []

        async def close(self):
            return None

    async def fake_stream(question, context=""):
        yield realtime_module.llm.LLMStreamPart(text=f"回答:{question}")

    monkeypatch.setattr(realtime_module.asr, "FunAsrStream", FakeStream)
    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    pipeline = RealtimePipeline(capture_broadcast)
    start = datetime.now(timezone.utc)
    for seq in (0, 1):
        item = AudioWork(
            session_id=session["id"],
            chunk_id=str(uuid4()),
            source="pc",
            codec="wav_pcm_s16le",
            chunk_seq=seq,
            captured_at=start + timedelta(milliseconds=seq * 500),
            duration_ms=1000,
            audio_bytes=b"wav",
        )
        accepted, _ = await pipeline.enqueue_audio(item)
        assert accepted

    await asyncio.sleep(0.1)
    assert any(message["type"] == "transcript_partial" for message in messages)
    for _ in range(100):
        conn = db.get_db()
        try:
            released = conn.execute(
                "SELECT COUNT(*) FROM audio_chunks WHERE session_id = ? AND status = 'done'",
                (session["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        if released == 2:
            break
        await asyncio.sleep(0.01)
    conn = db.get_db()
    try:
        assert [item["text"] for item in db.get_transcripts(conn, session["id"])] == [
            "最终切片1",
            "最终切片2",
        ]
        chunk_statuses = conn.execute(
            "SELECT chunk_seq, status FROM audio_chunks WHERE session_id = ? ORDER BY chunk_seq",
            (session["id"],),
        ).fetchall()
        assert [tuple(row) for row in chunk_statuses] == [(0, "done"), (1, "done")]
    finally:
        conn.close()

    for _ in range(100):
        if any(message["type"] == "answer" for message in messages):
            break
        await asyncio.sleep(0.01)
    assert any(
        message["type"] == "answer"
        and message["question"] in {"最终切片1", "最终切片2"}
        for message in messages
    )
    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
        transcripts = db.get_transcripts(conn, session["id"])
    finally:
        conn.close()
    assert [item["text"] for item in transcripts] == ["最终切片1", "最终切片2"]
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_automatic_answers_run_concurrently_and_all_are_persisted(monkeypatch):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "低延迟答案续写")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    both_started = asyncio.Event()
    release = asyncio.Event()
    started_questions = set()
    messages = []

    async def fake_stream(question, context=""):
        started_questions.add(question)
        if len(started_questions) == 2:
            both_started.set()
        yield realtime_module.llm.LLMStreamPart(text=f"{question}的开头")
        await release.wait()
        yield realtime_module.llm.LLMStreamPart(text="与结尾")

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)
    monkeypatch.setenv("AI_LLM_SESSION_MAX_CONCURRENCY", "2")
    pipeline = RealtimePipeline(capture_broadcast)
    assert await pipeline.enqueue_answer(
        AnswerWork(session["id"], "第一片", False)
    )
    assert await pipeline.enqueue_answer(
        AnswerWork(session["id"], "第二片", False)
    )
    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()

    for _ in range(100):
        if len([message for message in messages if message.get("type") == "answer"]) == 2:
            break
        await asyncio.sleep(0.01)

    conn = db.get_db()
    try:
        answers = db.get_answers(conn, session["id"])
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    assert {answer["question"] for answer in answers} == {"第一片", "第二片"}
    stream_request_ids = {
        message["request_id"]
        for message in messages
        if message.get("type") == "answer_stream"
    }
    final_request_ids = {
        message["request_id"] for message in messages if message.get("type") == "answer"
    }
    assert len(stream_request_ids) == 2
    assert final_request_ids == stream_request_ids
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_session_stop_does_not_roll_back_a_durable_transcript(monkeypatch):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "持久化完成后的取消竞态")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    transcript_broadcast_started = asyncio.Event()

    async def blocking_broadcast(_session_id, message):
        if message["type"] == "transcript":
            transcript_broadcast_started.set()
            await asyncio.Event().wait()

    async def fake_transcribe(*_args):
        return "已经持久化的转写"

    monkeypatch.setattr(asr, "transcribe_audio", fake_transcribe)
    pipeline = RealtimePipeline(blocking_broadcast)
    chunk_id = str(uuid4())
    accepted, _ = await pipeline.enqueue_audio(
        AudioWork(
            session_id=session["id"],
            chunk_id=chunk_id,
            source="pc",
            codec="webm_opus",
            chunk_seq=0,
            captured_at=datetime.now(timezone.utc),
            duration_ms=1000,
            audio_bytes=b"audio",
        )
    )
    assert accepted
    await asyncio.wait_for(transcript_broadcast_started.wait(), timeout=1)

    conn = db.get_db()
    try:
        before_stop = conn.execute(
            "SELECT status, transcript_id FROM audio_chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    assert tuple(before_stop) == ("done", 1)

    await pipeline.stop_session(session["id"])

    conn = db.get_db()
    try:
        after_stop = conn.execute(
            "SELECT status, transcript_id, error_code FROM audio_chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        transcripts = db.get_transcripts(conn, session["id"])
    finally:
        conn.close()
    assert tuple(after_stop) == ("done", 1, None)
    assert [(item["chunk_id"], item["text"]) for item in transcripts] == [
        (chunk_id, "已经持久化的转写")
    ]


@pytest.mark.asyncio
async def test_cancel_audio_source_cancels_current_and_queue_but_keeps_newer_chunk(
    monkeypatch,
):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "来源取消当前与队列")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    current_started = asyncio.Event()
    current_cancelled = asyncio.Event()
    broadcasts = []

    async def fake_transcribe(audio_bytes, *_args):
        if audio_bytes == b"zero":
            current_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                current_cancelled.set()
                raise
        return audio_bytes.decode()

    async def collect_broadcast(_session_id, message):
        broadcasts.append(message)

    monkeypatch.setattr(asr, "transcribe_audio", fake_transcribe)
    pipeline = RealtimePipeline(collect_broadcast)
    for seq, payload in enumerate((b"zero", b"one", b"two")):
        accepted, _ = await pipeline.enqueue_audio(
            _audio_work(session["id"], seq, payload)
        )
        assert accepted
        if seq == 0:
            await asyncio.wait_for(current_started.wait(), timeout=1)

    events = await pipeline.cancel_audio_source(
        session["id"], "pc", 1, "capture_stopped"
    )

    await asyncio.wait_for(current_cancelled.wait(), timeout=1)
    await _wait_for_chunk_status(session["id"], 2, "done")
    assert [event["payload"]["chunk_seq"] for event in events] == [0, 1]
    assert {
        message["chunk_seq"]
        for message in broadcasts
        if message["type"] == "chunk_ack"
        and message.get("status") == "cancelled"
    } == {0, 1}

    conn = db.get_db()
    try:
        chunks = db.get_audio_chunks(conn, session["id"])
        transcripts = db.get_transcripts(conn, session["id"])
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    assert [(chunk["chunk_seq"], chunk["status"]) for chunk in chunks] == [
        (0, "cancelled"),
        (1, "cancelled"),
        (2, "done"),
    ]
    assert [(item["chunk_seq"], item["text"]) for item in transcripts] == [
        (2, "two")
    ]
    key = (session["id"], "pc")
    for _ in range(100):
        if pipeline._audio_outstanding.get(key, 0) == 0:
            break
        await asyncio.sleep(0.01)
    assert pipeline._audio_outstanding.get(key, 0) == 0
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_cancel_audio_source_clears_pending_and_rejects_late_old_sequence(
    monkeypatch,
):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "来源取消 pending")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    transcribed = []

    async def fake_transcribe(audio_bytes, *_args):
        transcribed.append(audio_bytes.decode())
        return audio_bytes.decode()

    async def ignore_broadcast(_session_id, _message):
        return None

    monkeypatch.setattr(asr, "transcribe_audio", fake_transcribe)
    monkeypatch.setenv("AI_AUDIO_REORDER_WAIT_SECONDS", "5")
    pipeline = RealtimePipeline(ignore_broadcast)
    key = (session["id"], "pc")
    accepted, _ = await pipeline.enqueue_audio(_audio_work(session["id"], 2, b"two"))
    assert accepted
    for _ in range(100):
        if 2 in pipeline._audio_pending.get(key, {}):
            break
        await asyncio.sleep(0.01)
    assert 2 in pipeline._audio_pending.get(key, {})

    await pipeline.cancel_audio_source(session["id"], "pc", 2, "capture_stopped")
    accepted, cancelled_record = await pipeline.enqueue_audio(
        _audio_work(session["id"], 1, b"late-one")
    )
    assert accepted
    assert (
        cancelled_record["status"],
        cancelled_record["error_code"],
    ) == ("cancelled", "capture_stopped")

    accepted, _ = await pipeline.enqueue_audio(
        _audio_work(session["id"], 3, b"three")
    )
    assert accepted
    await _wait_for_chunk_status(session["id"], 3, "done")
    assert transcribed == ["three"]
    for _ in range(100):
        if pipeline._audio_outstanding.get(key, 0) == 0:
            break
        await asyncio.sleep(0.01)
    assert pipeline._audio_outstanding.get(key, 0) == 0

    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_cancel_audio_source_serializes_with_an_enqueue_already_reserving(
    monkeypatch,
):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "取消与 reserve 竞态")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    reserved = threading.Event()
    release_reservation = threading.Event()
    original_reserve = db.reserve_audio_chunk

    def delayed_reserve(conn, **kwargs):
        result = original_reserve(conn, **kwargs)
        reserved.set()
        release_reservation.wait(timeout=2)
        return result

    async def slow_transcribe(*_args):
        await asyncio.sleep(10)
        return "不应完成"

    async def ignore_broadcast(_session_id, _message):
        return None

    monkeypatch.setattr(db, "reserve_audio_chunk", delayed_reserve)
    monkeypatch.setattr(asr, "transcribe_audio", slow_transcribe)
    pipeline = RealtimePipeline(ignore_broadcast)
    enqueue_task = asyncio.create_task(
        pipeline.enqueue_audio(_audio_work(session["id"], 0, b"racing"))
    )
    for _ in range(100):
        if reserved.is_set():
            break
        await asyncio.sleep(0.01)
    assert reserved.is_set()

    cancel_task = asyncio.create_task(
        pipeline.cancel_audio_source(
            session["id"], "pc", 0, "capture_stopped"
        )
    )
    await asyncio.sleep(0)
    release_reservation.set()
    accepted, _ = await enqueue_task
    events = await cancel_task

    assert accepted
    assert [event["payload"]["status"] for event in events] == ["cancelled"]
    conn = db.get_db()
    try:
        chunks = db.get_audio_chunks(conn, session["id"])
        transcripts = db.get_transcripts(conn, session["id"])
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    assert [(chunk["chunk_seq"], chunk["status"]) for chunk in chunks] == [
        (0, "cancelled")
    ]
    assert transcripts == []
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_gap_abandon_requeue_does_not_leak_audio_outstanding(monkeypatch):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "缺口重入队计数")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    async def fake_transcribe(audio_bytes, *_args):
        return audio_bytes.decode()

    async def ignore_broadcast(_session_id, _message):
        return None

    monkeypatch.setattr(asr, "transcribe_audio", fake_transcribe)
    monkeypatch.setenv("AI_AUDIO_REORDER_WAIT_SECONDS", "0.01")
    pipeline = RealtimePipeline(ignore_broadcast)
    work = _audio_work(session["id"], 1, b"after-gap")
    accepted, _ = await pipeline.enqueue_audio(work)
    assert accepted
    await _wait_for_chunk_status(session["id"], 1, "failed")

    accepted, _ = await pipeline.enqueue_audio(work)
    assert accepted
    await _wait_for_chunk_status(session["id"], 1, "done")
    key = (session["id"], "pc")
    for _ in range(100):
        if pipeline._audio_outstanding.get(key, 0) == 0:
            break
        await asyncio.sleep(0.01)
    assert pipeline._audio_outstanding.get(key, 0) == 0

    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])
