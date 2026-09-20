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

    async def fake_stream(question, context="", *_ctx):
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
    # H1 delta-only 契约:token 帧只带 delta(全文 answer 恒为空串,twin 字段
    # text 已从协议删除),终止 done 帧单独携带全文一次。
    assert all("text" not in message for message in answer_stream_messages)
    token_frames = [
        message for message in answer_stream_messages if message["delta"]
    ]
    assert token_frames
    assert all(message["answer"] == "" for message in token_frames)
    done_frames = [
        message for message in answer_stream_messages if message["done"] is True
    ]
    assert len(done_frames) == 1
    assert done_frames[0]["answer"] == "先给出项目背景和技术方案。"
    assert done_frames[0]["delta"] == ""
    # 思考过程功能已下线:上游 reasoning 增量不再产生 channel="thinking" 广播,
    # answer 事件也不再携带 thinking 字段。
    assert all(message["channel"] == "answer" for message in stream_messages)
    assert all("thinking" not in message for message in stream_messages)
    assert stream_messages[-1]["done"] is True
    final_answer_event = next(message for message in messages if message.get("type") == "answer")
    assert "thinking" not in final_answer_event
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

    async def fake_search(question, context="", *_ctx):
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
async def test_revision_gate_is_geometric_with_sentence_end_override(monkeypatch):
    """revision 几何节流:纯追加的累计 partial 要长到上一版的 1.5 倍才开新火;
    追加部分出现句末标点则立即开火;ASR 改稿(非前缀)也立即开火。

    断言用 `thread.revision`：它只由闸门决定，和并发任务的完成顺序无关。
    """
    conn = db.get_db()
    try:
        session = db.create_session(conn, "revision 几何节流")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    async def fake_stream(question, context="", *_ctx):
        yield realtime_module.llm.LLMStreamPart(text=f"回答:{question}")

    async def capture_broadcast(_session_id, _message):
        return None

    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)
    pipeline = RealtimePipeline(capture_broadcast)
    key = (session["id"], "pc")
    segment = realtime_module.FunAsrSegment(session_id=key[0], source=key[1])

    # 7 版累计 partial(字符数 4/6/8/11/13/16/17),预期只开 4 火:
    #   6 字 = 4×1.5 边界开火;8 字 < 6×1.5=9 被闸;11 字 ≥ 9 开火;
    #   13、16 字都 < 11×1.5=16.5 被闸;17 字仍不足但追加带"？",句末标点开火。
    fired_revisions = []
    for text in (
        "请你介绍",
        "请你介绍一下",
        "请你介绍一下自己",
        "请你介绍一下自己的经历",
        "请你介绍一下自己的经历和",
        "请你介绍一下自己的经历和主要项目",
        "请你介绍一下自己的经历和主要项目？",
    ):
        await pipeline._dispatch_segment_revision(key, segment, text)
        fired_revisions.append(pipeline._question_threads[key].revision)
    assert fired_revisions == [1, 2, 2, 3, 3, 3, 4]
    # ASR 改稿:新文本不以旧文开头,立即开火,不能被倍率闸住。
    await pipeline._dispatch_segment_revision(key, segment, "换个话题")
    assert pipeline._question_threads[key].revision == 5
    await pipeline.stop_session(session["id"])
    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_funasr_cumulative_partial_drives_revisions_and_one_transcript_per_segment(
    monkeypatch,
):
    """开放式 utterance：分片推流即 ack done，累计 partial 逐版问 LLM，段末一条转写。"""
    conn = db.get_db()
    try:
        session = db.create_session(conn, "无问题检测实时流")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    monkeypatch.setenv("AI_FUNASR_STREAM", "true")
    monkeypatch.setenv("AI_QUESTION_THREAD_GRACE_SECONDS", "0.05")
    # 每片只多 3 个字;默认 1.5 倍几何闸门下 6 字 ≥ 3×1.5,两片各自触发一版,
    # 不再需要把增量闸门压到 1。
    messages = []

    class FakeStream:
        """模拟网关：partial 是当前 utterance 的累计全文，final 只在 stop 后出现。"""

        def __init__(self):
            self.push_count = 0
            self.text = ""

        async def connect(self):
            return None

        async def push_wav(self, _audio_bytes, _duration_ms):
            self.push_count += 1
            self.text += f"第{self.push_count}片"
            return [asr.FunAsrEvent("partial", text=self.text)]

        async def finish(self):
            if not self.text:
                return []
            return [asr.FunAsrEvent("final", text=self.text)]

        async def close(self):
            return None

    async def fake_stream(question, context="", *_ctx):
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
    # 累计 partial 每来一版都要立刻广播，客户端做整句替换显示。
    partial_texts = [
        message["text"] for message in messages if message["type"] == "transcript_partial"
    ]
    assert "第1片" in partial_texts
    assert "第1片第2片" in partial_texts
    # 分片一推进网关就 ack done，不等段末 final，否则长问题会顶满客户端发件箱。
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
        # 语音段还没结束，段末 final 还没产生，因此此刻不应有任何转写入库。
        assert db.get_transcripts(conn, session["id"]) == []
        chunk_statuses = conn.execute(
            "SELECT chunk_seq, status FROM audio_chunks WHERE session_id = ? ORDER BY chunk_seq",
            (session["id"],),
        ).fetchall()
        assert [tuple(row) for row in chunk_statuses] == [(0, "done"), (1, "done")]
    finally:
        conn.close()

    await pipeline.mark_speech_end(session["id"], "pc", 1)
    for _ in range(100):
        if any(message["type"] == "answer" for message in messages):
            break
        await asyncio.sleep(0.01)
    # 两片各触发一次 revision，段末 final 再强制一版；入库答案对应段末全文。
    assert any(
        message["type"] == "answer" and message["question"] == "第1片第2片"
        for message in messages
    )
    answer_questions = [
        message["question"] for message in messages if message["type"] == "answer_stream"
    ]
    assert "第1片" in answer_questions
    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
        transcripts = db.get_transcripts(conn, session["id"])
    finally:
        conn.close()
    # 一个语音段只落一条转写，而不是每片一条。
    assert [item["text"] for item in transcripts] == ["第1片第2片"]
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_question_revisions_run_concurrently_and_only_latest_is_persisted(monkeypatch):
    """同一问题的累计修订都执行，前端按 thread_id 聚合，最终只落最新版。"""
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

    async def fake_stream(question, context="", *_ctx):
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
    monkeypatch.setenv("AI_QUESTION_THREAD_GRACE_SECONDS", "0.05")
    monkeypatch.setenv("AI_QUESTION_THREAD_GRACE_SECONDS", "0.05")
    pipeline = RealtimePipeline(capture_broadcast)
    key = (session["id"], "pc")
    assert await pipeline._enqueue_question_revision(key, "浏览器输入 URL 后", 0)
    assert await pipeline._enqueue_question_revision(
        key, "浏览器输入 URL 后发生了什么？", 1
    )
    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert started_questions == {
        "浏览器输入 URL 后",
        "浏览器输入 URL 后发生了什么？",
    }

    await pipeline.mark_speech_end(session["id"], "pc", 1)
    release.set()

    for _ in range(100):
        if len([message for message in messages if message.get("type") == "answer"]) == 1:
            break
        await asyncio.sleep(0.01)

    conn = db.get_db()
    try:
        answers = db.get_answers(conn, session["id"])
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    assert [answer["question"] for answer in answers] == [
        "浏览器输入 URL 后发生了什么？"
    ]
    stream_request_ids = {
        message["request_id"]
        for message in messages
        if message.get("type") == "answer_stream"
    }
    stream_thread_ids = {
        message["thread_id"]
        for message in messages
        if message.get("type") == "answer_stream"
    }
    final = next(message for message in messages if message.get("type") == "answer")
    assert len(stream_request_ids) == 2
    assert len(stream_thread_ids) == 1
    assert final["thread_id"] in stream_thread_ids
    assert final["revision"] == 2
    assert final["request_id"] in stream_request_ids
    # C1:落库答案带线程身份,REST/回填才能挂回线程卡。
    assert answers[0]["thread_id"] == final["thread_id"]
    assert answers[0]["revision"] == 2
    assert answers[0]["request_id"] == final["request_id"]
    # H1:线程路径的 started 空帧保持原形状(delta/answer 均为空,无 text)。
    started_frames = [
        message
        for message in messages
        if message.get("type") == "answer_stream" and message.get("started") is True
    ]
    assert started_frames
    assert all(
        message["delta"] == "" and message["answer"] == "" and "text" not in message
        for message in started_frames
    )
    completed_revisions = {
        message["revision"]
        for message in messages
        if message.get("type") == "answer_stream"
        and message.get("done") is True
        and message.get("failed") is not True
    }
    assert completed_revisions == {1, 2}
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_speech_end_before_asr_final_still_closes_the_question_thread(monkeypatch):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "边界先于 final")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    messages = []

    async def fake_stream(question, context="", *_ctx):
        yield realtime_module.llm.LLMStreamPart(text=f"回答:{question}")

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)
    monkeypatch.setenv("AI_QUESTION_THREAD_GRACE_SECONDS", "0.05")
    pipeline = RealtimePipeline(capture_broadcast)
    await pipeline.mark_speech_end(session["id"], "pc", 0)
    assert await pipeline._enqueue_question_revision(
        (session["id"], "pc"), "DNS 查询过程是什么？", 0
    )

    for _ in range(100):
        if any(message.get("type") == "answer" for message in messages):
            break
        await asyncio.sleep(0.01)

    final = next(message for message in messages if message.get("type") == "answer")
    assert final["question"] == "DNS 查询过程是什么？"
    assert final["revision"] == 1
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_newer_chunk_within_grace_keeps_the_same_question_thread(monkeypatch):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "短暂停顿后续问")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    messages = []

    async def fake_stream(question, context="", *_ctx):
        yield realtime_module.llm.LLMStreamPart(text=f"回答:{question}")

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)
    monkeypatch.setenv("AI_QUESTION_THREAD_GRACE_SECONDS", "0.2")
    pipeline = RealtimePipeline(capture_broadcast)
    key = (session["id"], "pc")
    assert await pipeline._enqueue_question_revision(key, "Claude Code", 0)
    await pipeline.mark_speech_end(session["id"], "pc", 0)
    # 段末 final 会固化前缀；下一段的累计 partial 只覆盖新段，必须拼在前缀后面。
    await pipeline._commit_question_prefix(key)
    assert await pipeline._enqueue_question_revision(key, "和 Codex 的优劣势？", 1)
    await asyncio.sleep(0.25)
    assert not any(message.get("type") == "answer" for message in messages)

    await pipeline.mark_speech_end(session["id"], "pc", 1)
    for _ in range(100):
        if any(message.get("type") == "answer" for message in messages):
            break
        await asyncio.sleep(0.01)
    final = next(message for message in messages if message.get("type") == "answer")
    assert final["question"] == "Claude Code和 Codex 的优劣势？"
    assert final["revision"] == 2
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


@pytest.mark.asyncio
async def test_regenerate_thread_answer_reopens_a_closed_thread(monkeypatch):
    """重问某张问题卡:已关闭线程被重开,revision+1,新答案落库到同一线程。

    线程入库后保留在 by_id(regenerate 的前提),且可连续 regenerate。
    """
    conn = db.get_db()
    try:
        session = db.create_session(conn, "重新生成问题卡")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    messages = []

    async def fake_stream(question, context="", *_ctx):
        yield realtime_module.llm.LLMStreamPart(text=f"重答:{question}")

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)
    monkeypatch.setenv("AI_QUESTION_THREAD_GRACE_SECONDS", "0.05")
    pipeline = RealtimePipeline(capture_broadcast)
    key = (session["id"], "pc")

    # 走一遍正常提问→宽限关闭→落库。
    assert await pipeline._enqueue_question_revision(key, "讲讲缓存一致性", 0)
    await pipeline.mark_speech_end(session["id"], "pc", 0)

    def _final_answers():
        return [m for m in messages if m.get("type") == "answer" and m.get("thread_id")]

    # 落库先于 broadcast,轮询要等广播消息(比 DB 可见更晚)。
    for _ in range(100):
        if _final_answers():
            break
        await asyncio.sleep(0.01)
    final_messages = _final_answers()
    assert len(final_messages) == 1
    thread_id = final_messages[0]["thread_id"]
    assert final_messages[0]["revision"] == 1
    conn = db.get_db()
    try:
        answers = db.get_answers(conn, session["id"])
    finally:
        conn.close()
    assert len(answers) == 1
    # 关闭并入库后,线程必须仍保留在 by_id:否则 regenerate 没有靶子。
    assert thread_id in pipeline._question_threads_by_id
    assert pipeline._question_threads_by_id[thread_id].closed
    assert pipeline._question_threads_by_id[thread_id].persisted

    # regenerate:同线程重开,revision 递增,答案流回同 thread_id。
    assert await pipeline.regenerate_thread_answer(
        session["id"], thread_id, "讲讲缓存一致性", False
    )
    reopened = pipeline._question_threads_by_id[thread_id]
    assert reopened.revision == 2, "regenerate 必须在同一线程上递增 revision"
    assert reopened.persisted is False, "重开后的线程要重新等这次生成落库"

    for _ in range(100):
        if len(_final_answers()) == 2:
            break
        await asyncio.sleep(0.01)
    final_messages = _final_answers()
    assert [m["revision"] for m in final_messages] == [1, 2]
    conn = db.get_db()
    try:
        answers = db.get_answers(conn, session["id"])
    finally:
        conn.close()
    assert len(answers) == 2
    assert answers[1]["answer"].startswith("重答")
    assert {m["thread_id"] for m in final_messages} == {thread_id}
    stream_frames = [
        m
        for m in messages
        if m.get("type") == "answer_stream" and m.get("revision") == 2
    ]
    assert stream_frames, "重新生成的答案必须流回前端"

    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])
    # 会话结束后保留的线程必须清干净,否则整场的线程对象泄漏到进程退出。
    assert thread_id not in pipeline._question_threads_by_id


@pytest.mark.asyncio
async def test_failed_regenerate_does_not_duplicate_the_previous_persisted_answer(
    monkeypatch,
):
    conn = db.get_db()
    try:
        session = db.create_session(conn, "重新生成失败不重复历史")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    should_fail = False
    messages = []

    async def fake_stream(question, context="", *_ctx):
        if should_fail:
            raise RuntimeError("regenerate failed")
        yield realtime_module.llm.LLMStreamPart(text=f"首次:{question}")

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)
    monkeypatch.setenv("AI_QUESTION_THREAD_GRACE_SECONDS", "0.05")
    pipeline = RealtimePipeline(capture_broadcast)
    key = (session["id"], "pc")
    assert await pipeline._enqueue_question_revision(key, "什么是幂等？", 0)
    await pipeline.mark_speech_end(session["id"], "pc", 0)
    for _ in range(100):
        finals = [m for m in messages if m.get("type") == "answer"]
        if finals:
            break
        await asyncio.sleep(0.01)
    thread_id = finals[0]["thread_id"]

    should_fail = True
    assert await pipeline.regenerate_thread_answer(
        session["id"], thread_id, "什么是幂等？", False
    )
    for _ in range(100):
        if any(m.get("failed") is True for m in messages):
            break
        await asyncio.sleep(0.01)

    conn = db.get_db()
    try:
        answers = db.get_answers(conn, session["id"])
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    assert len(answers) == 1
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_regenerate_thread_answer_recreates_a_missing_thread_with_same_id(
    monkeypatch,
):
    """线程找不到(后端重启过)时以相同 thread_id 重建,答案照常落库。

    重建的线程只进 by_id,不进 key 映射——不打断同 key 上正在问的新问题。
    """
    conn = db.get_db()
    try:
        session = db.create_session(conn, "重建丢失的线程")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    async def fake_stream(question, context="", *_ctx):
        yield realtime_module.llm.LLMStreamPart(text=f"重建:{question}")

    messages = []

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)
    monkeypatch.setenv("AI_QUESTION_THREAD_GRACE_SECONDS", "0.05")
    pipeline = RealtimePipeline(capture_broadcast)
    orphan_id = str(uuid4())

    assert await pipeline.regenerate_thread_answer(
        session["id"], orphan_id, "重建后的问题", False
    )
    recreated = pipeline._question_threads_by_id.get(orphan_id)
    assert recreated is not None
    assert recreated.session_id == session["id"]
    assert recreated.revision == 1
    # 重建线程不占用 key 映射:该 key 上的新问题照常开自己的线程。
    assert (session["id"], "pc") not in pipeline._question_threads
    assert await pipeline._enqueue_question_revision(
        (session["id"], "pc"), "新问题", 0
    )
    new_thread = pipeline._question_threads[(session["id"], "pc")]
    assert new_thread.thread_id != orphan_id
    # 新问题走正常语音段收尾,宽限期后落库。
    await pipeline.mark_speech_end(session["id"], "pc", 0)

    # 重建线程的答案按原 thread_id 落库(事件载荷),新问题落自己的新 id。
    for _ in range(100):
        finals = [m for m in messages if m.get("type") == "answer" and m.get("thread_id")]
        if len(finals) == 2:
            break
        await asyncio.sleep(0.01)
    by_thread = {m["thread_id"]: m for m in finals}
    assert orphan_id in by_thread
    assert by_thread[orphan_id]["answer"].startswith("重建")
    assert new_thread.thread_id in by_thread

    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_one_revision_failure_does_not_cancel_a_newer_revision(monkeypatch):
    """并发修订互不取消：旧版失败可见，新版仍完成并成为落库答案。"""
    conn = db.get_db()
    try:
        session = db.create_session(conn, "取消伪装成传输错误")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    both_started = asyncio.Event()
    release = asyncio.Event()
    messages = []
    started = set()

    async def fake_stream(question, context="", *_ctx):
        started.add(question)
        if len(started) == 2:
            both_started.set()
        if question == "旧问题":
            yield realtime_module.llm.LLMStreamPart(text="半截")
            await release.wait()
            raise RuntimeError("peer closed connection without response")
        await release.wait()
        yield realtime_module.llm.LLMStreamPart(text="新版完整答案")

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)
    monkeypatch.setenv("AI_LLM_SESSION_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("AI_QUESTION_THREAD_GRACE_SECONDS", "0.05")
    pipeline = RealtimePipeline(capture_broadcast)
    key = (session["id"], "pc")
    assert await pipeline._enqueue_question_revision(key, "旧问题", 0)
    assert await pipeline._enqueue_question_revision(key, "旧问题完整版？", 1)
    await asyncio.wait_for(both_started.wait(), timeout=1)
    await pipeline.mark_speech_end(session["id"], "pc", 1)
    release.set()

    for _ in range(100):
        done_new = [
            m
            for m in messages
            if m.get("type") == "answer_stream"
            and m.get("revision") == 2
            and m.get("done") is True
        ]
        if done_new:
            break
        await asyncio.sleep(0.01)
    assert done_new, "新版要正常收尾"

    failed_frames = [
        m
        for m in messages
        if m.get("type") == "answer_stream" and m.get("revision") == 1 and m.get("failed")
    ]
    assert failed_frames, "旧版真实失败应只标记自己的 revision"
    # H1:failed 终止帧携带已流出的部分全文,token 帧不重复全文。
    assert failed_frames[0]["answer"] == "半截"
    assert "text" not in failed_frames[0]
    assert [
        m for m in messages if m.get("code") == "answer_generation_failed"
    ]

    for _ in range(100):
        finals = [m for m in messages if m.get("type") == "answer"]
        if finals:
            break
        await asyncio.sleep(0.01)
    assert len(finals) == 1
    assert finals[0]["revision"] == 2
    assert finals[0]["answer"] == "新版完整答案"

    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_nonstreaming_finals_commit_prefix_exactly_once(monkeypatch):
    """非流式路径连续 3 片 final:前缀只在 _enqueue_question_revision 内拼一次。

    回归:此前 _persist_single_final 先把 committed_prefix 拼进 cumulative,
    _enqueue_question_revision 内部又拼一次,问题文本随分片数指数膨胀。
    """
    conn = db.get_db()
    try:
        session = db.create_session(conn, "非流式前缀单次拼接")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    async def fake_transcribe(audio_bytes, *_args):
        return audio_bytes.decode()

    async def fake_stream(question, context="", *_ctx):
        yield realtime_module.llm.LLMStreamPart(text=f"回答:{question}")

    messages = []

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    monkeypatch.setattr(asr, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)
    monkeypatch.setenv("AI_QUESTION_THREAD_GRACE_SECONDS", "0.05")
    pipeline = RealtimePipeline(capture_broadcast)
    key = (session["id"], "pc")
    captured_at = datetime(2026, 9, 20, tzinfo=timezone.utc)
    texts = ("介绍一下分布式锁", "的实现原理", "以及常见误区")
    expected_prefixes = [
        "介绍一下分布式锁",
        "介绍一下分布式锁的实现原理",
        "介绍一下分布式锁的实现原理以及常见误区",
    ]
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
        expected = expected_prefixes[seq]
        for _ in range(200):
            thread = pipeline._question_threads.get(key)
            if thread is not None and thread.committed_prefix == expected:
                break
            await asyncio.sleep(0.01)
        else:
            thread = pipeline._question_threads.get(key)
            pytest.fail(
                f"chunk {seq} 固化前缀错误: {thread.committed_prefix if thread else None!r}"
            )

    await pipeline.mark_speech_end(session["id"], "pc", 2)
    for _ in range(100):
        if any(m.get("type") == "answer" for m in messages):
            break
        await asyncio.sleep(0.01)
    final = next(m for m in messages if m.get("type") == "answer")
    assert final["question"] == "介绍一下分布式锁的实现原理以及常见误区"
    await pipeline.stop_session(session["id"])


@pytest.mark.asyncio
async def test_flush_and_stop_bound_a_wedged_processing_task(monkeypatch):
    """FunASR TCP 卡死(转写任务永不完成)时 flush 等待有界,stop 会取消它。"""
    conn = db.get_db()
    try:
        session = db.create_session(conn, "转写卡死")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    async def ignore_broadcast(_session_id, _message):
        return None

    monkeypatch.setenv("AI_FINAL_ANSWER_FLUSH_TIMEOUT_SECONDS", "0.2")
    pipeline = RealtimePipeline(ignore_broadcast)
    key = (session["id"], "pc")
    wedged = asyncio.create_task(asyncio.sleep(30))
    pipeline._audio_processing_tasks[key] = wedged

    async def flush_and_stop():
        await pipeline.flush_session(session["id"])
        await pipeline.stop_session(session["id"])

    await asyncio.wait_for(flush_and_stop(), timeout=2)
    assert wedged.cancelled()
    assert key not in pipeline._audio_processing_tasks


@pytest.mark.asyncio
async def test_end_session_during_slow_stream_still_persists_answer(monkeypatch):
    """end_session 撞上还在流的答案:只要流在 LLM 超时内正常收尾就要落库。"""
    conn = db.get_db()
    try:
        session = db.create_session(conn, "慢流答案")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    first_delta_seen = asyncio.Event()

    async def slow_stream(question, context="", *_ctx):
        yield realtime_module.llm.LLMStreamPart(text="第一段")
        first_delta_seen.set()
        # 超过 flush 超时(0.2s),但远低于 LLM 流超时(60s)
        await asyncio.sleep(1)
        yield realtime_module.llm.LLMStreamPart(text="第二段")

    async def capture_broadcast(_session_id, _message):
        return None

    monkeypatch.setattr(realtime_module.llm, "stream_answer", slow_stream)
    monkeypatch.setenv("AI_FINAL_ANSWER_FLUSH_TIMEOUT_SECONDS", "0.2")
    pipeline = RealtimePipeline(capture_broadcast)
    key = (session["id"], "pc")
    assert await pipeline._enqueue_question_revision(key, "慢问题？", 0)
    await asyncio.wait_for(first_delta_seen.wait(), timeout=1)

    # 复刻 end_session 路由的顺序:flush → 落库结束状态 → stop。
    await pipeline.flush_session(session["id"])
    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])

    conn = db.get_db()
    try:
        answers = db.get_answers(conn, session["id"])
    finally:
        conn.close()
    assert [(answer["question"], answer["answer"]) for answer in answers] == [
        ("慢问题？", "第一段第二段")
    ]


@pytest.mark.asyncio
async def test_unfinishable_stream_persists_partial_answer(monkeypatch):
    """等满 LLM 流超时仍不收尾的生成:取消后已生成的部分答案要落库,不得无声丢失。"""
    conn = db.get_db()
    try:
        session = db.create_session(conn, "卡死流部分答案")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    first_delta_seen = asyncio.Event()
    release = asyncio.Event()

    async def stuck_stream(question, context="", *_ctx):
        yield realtime_module.llm.LLMStreamPart(text="已生成的部分")
        first_delta_seen.set()
        await release.wait()

    messages = []

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    monkeypatch.setattr(realtime_module.llm, "stream_answer", stuck_stream)
    monkeypatch.setenv("AI_FINAL_ANSWER_FLUSH_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setattr(realtime_module.llm, "STREAM_TIMEOUT_SECONDS", 0.3)
    pipeline = RealtimePipeline(capture_broadcast)
    key = (session["id"], "pc")
    assert await pipeline._enqueue_question_revision(key, "卡死问题？", 0)
    await asyncio.wait_for(first_delta_seen.wait(), timeout=1)

    await pipeline.flush_session(session["id"])
    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])

    conn = db.get_db()
    try:
        answers = db.get_answers(conn, session["id"])
    finally:
        conn.close()
    assert [(answer["question"], answer["answer"]) for answer in answers] == [
        ("卡死问题？", "已生成的部分")
    ]
    final = next(m for m in messages if m.get("type") == "answer")
    assert final["answer"] == "已生成的部分"
    assert final["revision"] == 1


@pytest.mark.asyncio
async def test_partial_answer_falls_back_when_top_revision_has_no_text(monkeypatch):
    """最高 revision 在首 delta 前被取消:线程退回次高的非空版,不能整条丢失。"""
    conn = db.get_db()
    try:
        session = db.create_session(conn, "空高版本部分答案")
        db.start_session(conn, session["id"], "pc")
    finally:
        conn.close()

    first_delta_seen = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def fake_stream(question, context="", *_ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            yield realtime_module.llm.LLMStreamPart(text="低版本答案")
            first_delta_seen.set()
        await release.wait()

    messages = []

    async def capture_broadcast(_session_id, message):
        messages.append(message)

    monkeypatch.setattr(realtime_module.llm, "stream_answer", fake_stream)
    monkeypatch.setenv("AI_FINAL_ANSWER_FLUSH_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setattr(realtime_module.llm, "STREAM_TIMEOUT_SECONDS", 0.3)
    pipeline = RealtimePipeline(capture_broadcast)
    key = (session["id"], "pc")
    assert await pipeline._enqueue_question_revision(key, "修订问题？")
    await asyncio.wait_for(first_delta_seen.wait(), timeout=1)
    assert await pipeline._enqueue_question_revision(key, "修订问题？续")
    for _ in range(100):
        if len(pipeline._answer_stream_captures) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(pipeline._answer_stream_captures) == 2

    await pipeline.flush_session(session["id"])
    conn = db.get_db()
    try:
        db.end_session(conn, session["id"])
    finally:
        conn.close()
    await pipeline.stop_session(session["id"])

    conn = db.get_db()
    try:
        answers = db.get_answers(conn, session["id"])
    finally:
        conn.close()
    assert [(answer["question"], answer["answer"]) for answer in answers] == [
        ("修订问题？", "低版本答案")
    ]
    final = next(m for m in messages if m.get("type") == "answer")
    assert final["answer"] == "低版本答案"
    assert final["revision"] == 1
