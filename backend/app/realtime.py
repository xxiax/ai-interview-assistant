"""实时处理流水线：有序音频队列、FunASR 流、流式答案和答案队列。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from . import asr, cost_control, db, llm
from .protocol import event_message, server_message

logger = logging.getLogger(__name__)
Broadcast = Callable[[str, dict], Awaitable[None]]
_AUDIO_WAKE = object()


class _AudioSourceCancelled(RuntimeError):
    pass


# 片间文本归一化参数：分片独立转写(FunASR final 只覆盖本片)时，
# 相邻片常出现与前文尾部的重叠(同一音节被两片各转一次)。
_OVERLAP_MIN_CHARS = 4
_LAST_TAIL_CHARS = 32
_CJK_LEAD = re.compile(r"[一-鿿，。！？；：、）)》】]")
_SENTENCE_END = re.compile(r"[。！？!?；;…]")


def _revision_growth_ratio() -> float:
    """累计 partial 的字符数至少要增长到上一次提交 LLM 版本的这个倍数才开新 revision。

    几何节流：固定增量(旧 `AI_QUESTION_MIN_REVISION_DELTA_CHARS=4`)对短句太吵、
    对长句又太密——60 字的问题能刷十几个 revision。按倍数增长后 3 字起问、
    后续约 4/6/9/14/21 字各一版，整题 revision 数收敛到对数级；句末标点
    (`。！？!?；;…`)一出现仍立刻开新版，保证问完一句就有一版答案在跑。
    读环境变量而非模块常量，便于按部署和测试调整。
    """
    raw = os.environ.get("AI_QUESTION_REVISION_GROWTH_RATIO", "").strip()
    if not raw:
        return 1.5
    try:
        value = float(raw)
    except ValueError:
        return 1.5
    return max(1.0, value)


def normalize_segment_text(prev_text_tail: str, new_text: str) -> str:
    """把新分片转写归一化后返回应入库的文本。

    纯函数、无副作用；调用方维护每个 (session, source) 的上一条尾部缓存。

    - (a) 去重：new_text 的前缀与 prev 尾部有 ≥4 字符重叠时去掉重叠部分；
      取最长重叠，避免重复的中文开头(如"嗯/就是"碎片)被双写。
    - (b) 拼接语义见 join_transcript_text：中文句读延续不加空格、英文加空格。
    - (c) 片级 final 以句中字符结尾属于正常情况，无需特殊处理；
      下一片到来时按 (b) 自然拼接，句子边界因此恢复连续。
    """
    new_text = new_text.strip()
    prev_tail = prev_text_tail.strip()
    if not new_text:
        return ""
    if not prev_tail:
        return new_text

    overlap = 0
    max_overlap = min(len(prev_tail), len(new_text))
    for size in range(max_overlap, _OVERLAP_MIN_CHARS - 1, -1):
        if prev_tail[-size:] == new_text[:size]:
            overlap = size
            break
    if overlap:
        new_text = new_text[overlap:].strip()
        if not new_text:
            return ""
    return new_text


def join_transcript_text(prev: str, new: str) -> str:
    """按句读延续规则拼接两段转写文本。

    - new 首字符是中文(或中文标点)且 prev 不以句末标点结尾：句未断，
      直接拼接不补空格，恢复被分片边界切开的中文句子。
    - 其余情况(英文词、prev 已到句末)拼接时补一个空格。
    """
    prev = prev.strip()
    new = new.strip()
    if not prev:
        return new
    if not new:
        return prev
    if _CJK_LEAD.match(new[0]) and not _SENTENCE_END.search(prev[-1]):
        return f"{prev}{new}"
    return f"{prev} {new}"


def _is_config_error(exc: BaseException) -> bool:
    """判断 ASR 异常是否为不可重试的配置类错误。

    首选结构化类型（asr.AsrConfigError / asr.AsrAuthError，由 transcribe
    内部按 HTTP 状态码抛出）；字符串匹配仅兜底旧异常形状——此前靠
    "401 in str(exc) and 'groq.com' in ..." 判定，FunASR 的 401 不含
    groq.com 会被误分类为可重试的 processing_failed。
    """
    if isinstance(exc, (asr.AsrConfigError, asr.AsrAuthError)):
        return True
    reason = str(exc)
    if "未配置" in reason or "GROQ_API_KEY" in reason:
        return True
    # 旧形状兜底：httpx HTTPStatusError 的消息含状态码与 URL
    return ("401" in reason or "403" in reason) and "groq.com" in reason


def _audio_config_error_message() -> str:
    engine = os.environ.get("AI_ASR_ENGINE", "funasr").strip().lower()
    if engine == "funasr":
        return "备用 FunASR 不可用(Token 无效或无权限),请检查 FunASR 配置"
    if engine == "groq":
        return "备用 Groq 转写不可用(API Key 无效或无权限),请检查 Groq 配置"
    return "多模态 AI 转写不可用(API Key 无效或无权限),请在「设置」页检查 LLM 配置和模型能力"


def _audio_runtime_error_message() -> str:
    engine = os.environ.get("AI_ASR_ENGINE", "funasr").strip().lower()
    if engine == "funasr":
        return "FunASR 连接失败，请检查网络或代理配置"
    if engine == "groq":
        return "Groq 转写请求失败，请检查网络或 Groq 配置"
    return "多模态 AI 转写请求失败，请检查网络或 LLM 配置"


def _db_call(func, *args, **kwargs):
    conn = db.get_db()
    try:
        return func(conn, *args, **kwargs)
    finally:
        conn.close()


async def run_db(func, *args, **kwargs):
    """把同步 SQLite 操作移出事件循环。"""
    return await asyncio.to_thread(_db_call, func, *args, **kwargs)


@dataclass(frozen=True)
class AudioWork:
    session_id: str
    chunk_id: str
    source: str
    codec: str
    chunk_seq: int
    captured_at: datetime
    duration_ms: int
    audio_bytes: bytes


@dataclass(frozen=True)
class AnswerWork:
    session_id: str
    question: str
    use_search: bool
    request_id: str = field(default_factory=lambda: str(uuid4()))
    thread_id: str | None = None
    revision: int = 1
    persist_immediately: bool = True
    # 笔试辅助：非空时走多模态解题而不是文本提词。截图只在内存里流转，
    # 不进 SQLite、不进事件流——落库的只有生成出来的答案文本。
    image_bytes: bytes | None = None
    image_mime: str = "image/png"


@dataclass(frozen=True)
class AnswerCompletion:
    request_id: str
    revision: int
    question: str
    answer: str
    source: str


@dataclass
class _AnswerStreamCapture:
    """在途答案流的最新进度，供会话结束时兜底落库部分答案。"""

    item: AnswerWork
    text: str = ""
    source: str = "llm"


@dataclass
class QuestionThread:
    session_id: str
    source: str
    thread_id: str = field(default_factory=lambda: str(uuid4()))
    question: str = ""
    # 本线程内已经固化(段末 final)的问题前缀。宽限期内说话人接着说时,新语音段
    # 的累计 partial 拼在这个前缀后面,避免第二段把第一段完整问题覆盖掉。
    committed_prefix: str = ""
    revision: int = 0
    pending_revisions: set[int] = field(default_factory=set)
    latest_completion: AnswerCompletion | None = None
    close_task: asyncio.Task | None = None
    closed: bool = False
    persisted: bool = False
    persisting: bool = False


@dataclass
class FunAsrSegment:
    """一个进行中的 FunASR 语音段(utterance)。

    一个语音段对应客户端两次 `speech_end` 之间的所有分片:`start` 只在第一片
    发一次,`stop` 只在 `speech_end` 边界发一次。段内每次拿到新的累计 partial
    就开一个新 revision 并发提交 LLM,因此"先问一部分"不必等段末 final。
    """

    session_id: str
    source: str
    # 段内已推给 FunASR 的分片(仅用于失败时批量标记 failed)
    chunks: list[AudioWork] = field(default_factory=list)
    # 段内最后一片,段末 final 落库时绑定到它
    last_chunk: AudioWork | None = None
    # 已经用来触发 LLM 的累计 partial 全文,用于最小增量判定
    dispatched_text: str = ""


class RealtimePipeline:
    """按 session/source 顺序处理音频，并发生成累计问题的各个修订答案。"""

    def __init__(self, broadcast: Broadcast) -> None:
        self.broadcast = broadcast
        self.audio_queue_size = int(os.environ.get("AI_AUDIO_QUEUE_SIZE", "8"))
        self.answer_queue_size = int(os.environ.get("AI_ANSWER_QUEUE_SIZE", "8"))
        self.answer_concurrency = int(
            os.environ.get("AI_LLM_SESSION_MAX_CONCURRENCY", "3")
        )
        self._audio_queues: dict[tuple[str, str], asyncio.Queue[object]] = {}
        self._audio_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._audio_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._audio_outstanding: dict[tuple[str, str], int] = {}
        self._audio_next_seq: dict[tuple[str, str], int] = {}
        self._audio_pending: dict[tuple[str, str], dict[int, AudioWork]] = {}
        self._audio_current: dict[tuple[str, str], AudioWork] = {}
        self._audio_processing_tasks: dict[
            tuple[str, str], asyncio.Task[str]
        ] = {}
        self._audio_cancel_watermarks: dict[tuple[str, str], int] = {}
        self._audio_cancel_reasons: dict[tuple[str, str], tuple[int, str]] = {}
        # 每个 (会话,来源) 的"同一序号连续缺口超时"计数:≥2 判定洞被放弃并跳过
        self._gap_timeouts: dict[tuple[str, str], dict[int, int]] = {}
        # 每个 (会话,来源) 上一条已入库转写的尾部(≤32 字符)，供片间归一化
        self._transcript_tails: dict[tuple[str, str], str] = {}
        self._answer_queues: dict[str, asyncio.Queue[AnswerWork]] = {}
        self._answer_tasks: dict[str, asyncio.Task] = {}
        self._answer_generation_tasks: dict[str, set[asyncio.Task]] = {}
        # 在途答案流的最新进度(request_id → capture)。flush_session 等待超时被迫
        # 取消生成时,用它把已生成的部分答案落库,避免已消耗预算的答案无声丢失。
        self._answer_stream_captures: dict[str, _AnswerStreamCapture] = {}
        self._stopped_sessions: set[str] = set()
        self._stop_reasons: dict[str, str] = {}
        self._session_mutations: dict[str, int] = {}
        self._funasr_streams: dict[tuple[str, str], asr.FunAsrStream] = {}
        # 每个 (会话,来源) 进行中的语音段;None 表示当前没有打开的 utterance
        self._funasr_segments: dict[tuple[str, str], FunAsrSegment] = {}
        # speech_end 到达但边界分片还没推给 FunASR 时,记下水位待音频 worker 收尾
        self._funasr_pending_finish: dict[tuple[str, str], int] = {}
        self._funasr_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._question_threads: dict[tuple[str, str], QuestionThread] = {}
        self._question_threads_by_id: dict[str, QuestionThread] = {}
        self._question_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._speech_end_boundaries: dict[tuple[str, str], tuple[int, float]] = {}

    def prepare_for_current_loop(self) -> None:
        """应用生命周期重启时清除属于已关闭事件循环的运行态。"""
        current_loop = asyncio.get_running_loop()
        tasks = [*self._audio_tasks.values(), *self._answer_tasks.values()]
        stale = [task for task in tasks if task.get_loop() is not current_loop]
        if any(not task.done() and not task.get_loop().is_closed() for task in stale):
            raise RuntimeError("实时管线仍在另一个活动事件循环中运行")
        if stale or not tasks:
            self._audio_queues.clear()
            self._audio_tasks.clear()
            self._audio_locks.clear()
            self._audio_outstanding.clear()
            self._audio_next_seq.clear()
            self._audio_pending.clear()
            self._audio_current.clear()
            self._audio_processing_tasks.clear()
            self._audio_cancel_watermarks.clear()
            self._audio_cancel_reasons.clear()
            self._gap_timeouts.clear()
            self._transcript_tails.clear()
            self._answer_queues.clear()
            self._answer_tasks.clear()
            self._answer_generation_tasks.clear()
            self._answer_stream_captures.clear()
            self._stopped_sessions.clear()
            self._stop_reasons.clear()
            self._session_mutations.clear()
            self._funasr_streams.clear()
            self._funasr_segments.clear()
            self._funasr_pending_finish.clear()
            self._funasr_locks.clear()
            self._question_threads.clear()
            self._question_threads_by_id.clear()
            self._question_locks.clear()
            self._speech_end_boundaries.clear()

    async def _reject_stopped_session(self, session_id: str) -> None:
        if session_id not in self._stopped_sessions:
            return
        session = await run_db(db.require_session, session_id)
        raise db.SessionStateError(session["status"], "recording")

    def _begin_session_mutation(self, session_id: str) -> None:
        self._session_mutations[session_id] = (
            self._session_mutations.get(session_id, 0) + 1
        )

    def _finish_session_mutation(self, session_id: str) -> None:
        remaining = self._session_mutations.get(session_id, 0) - 1
        if remaining > 0:
            self._session_mutations[session_id] = remaining
            return
        self._session_mutations.pop(session_id, None)
        if session_id not in self._stop_reasons:
            self._stopped_sessions.discard(session_id)

    def _release_stopped_session_if_idle(self, session_id: str) -> None:
        if (
            self._session_mutations.get(session_id, 0) == 0
            and session_id not in self._stop_reasons
        ):
            self._stopped_sessions.discard(session_id)

    def _audio_queue(self, key: tuple[str, str]) -> asyncio.Queue[object]:
        queue = self._audio_queues.get(key)
        if queue is None:
            queue = asyncio.Queue(maxsize=self.audio_queue_size)
            self._audio_queues[key] = queue
            self._audio_outstanding.setdefault(key, 0)
        task = self._audio_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._audio_worker(key, queue), name=f"audio:{key[0]}:{key[1]}"
            )
            self._audio_tasks[key] = task
        return queue

    def _audio_is_cancelled(self, key: tuple[str, str], chunk_seq: int) -> bool:
        return chunk_seq <= self._audio_cancel_watermarks.get(key, -1)

    def _finish_audio_item(
        self, key: tuple[str, str], queue: asyncio.Queue[object]
    ) -> None:
        queue.task_done()
        self._audio_outstanding[key] = max(
            0, self._audio_outstanding.get(key, 0) - 1
        )

    @staticmethod
    def _discard_audio_wakes(queue: asyncio.Queue[object]) -> None:
        preserved = []
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            queue.task_done()
            if item is not _AUDIO_WAKE:
                preserved.append(item)
        for item in preserved:
            queue.put_nowait(item)

    def _answer_queue(self, session_id: str) -> asyncio.Queue[AnswerWork]:
        queue = self._answer_queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=self.answer_queue_size)
            self._answer_queues[session_id] = queue
        task = self._answer_tasks.get(session_id)
        if task is None or task.done():
            task = asyncio.create_task(
                self._answer_worker(session_id, queue), name=f"answer:{session_id}"
            )
            self._answer_tasks[session_id] = task
        return queue

    async def enqueue_audio(self, item: AudioWork) -> tuple[bool, dict]:
        key = (item.session_id, item.source)
        self._begin_session_mutation(item.session_id)
        try:
            await self._reject_stopped_session(item.session_id)
            lock = self._audio_locks.setdefault(key, asyncio.Lock())
            async with lock:
                cancelled = self._audio_is_cancelled(key, item.chunk_seq)
                if (
                    not cancelled
                    and self._audio_outstanding.get(key, 0) >= self.audio_queue_size
                ):
                    return False, {
                        "status": "backpressure",
                        "chunk_id": item.chunk_id,
                    }
                accepted, record = await run_db(
                    db.reserve_audio_chunk,
                    chunk_id=item.chunk_id,
                    session_id=item.session_id,
                    source=item.source,
                    codec=item.codec,
                    chunk_seq=item.chunk_seq,
                    captured_at=item.captured_at.isoformat(),
                    duration_ms=item.duration_ms,
                    content_sha256=hashlib.sha256(item.audio_bytes).hexdigest(),
                )
                if accepted:
                    if item.session_id in self._stopped_sessions:
                        await run_db(
                            db.mark_audio_chunk_status,
                            item.chunk_id,
                            "cancelled",
                            error_code="session_ended",
                        )
                        await self._reject_stopped_session(item.session_id)
                    if cancelled:
                        _, reason = self._audio_cancel_reasons[key]
                        events = await run_db(
                            db.cancel_audio_source_chunks,
                            item.session_id,
                            item.source,
                            item.chunk_seq,
                            reason,
                        )
                        matching = next(
                            (
                                event
                                for event in reversed(events)
                                if event["payload"]["chunk_id"] == item.chunk_id
                            ),
                            None,
                        )
                        record = {
                            **record,
                            "status": "cancelled",
                            "error_code": reason,
                            "transcript_id": None,
                        }
                        if matching:
                            record["event_id"] = matching["event_id"]
                    else:
                        queue = self._audio_queue(key)
                        self._discard_audio_wakes(queue)
                        self._audio_outstanding[key] = (
                            self._audio_outstanding.get(key, 0) + 1
                        )
                        queue.put_nowait(item)
                return accepted, record
        finally:
            self._finish_session_mutation(item.session_id)

    async def cancel_audio_source(
        self,
        session_id: str,
        source: str,
        through_chunk_seq: int,
        reason: str,
    ) -> list[dict]:
        """线性化取消单一音频来源的水位内分片，并保留更高序号工作。"""
        if source not in db.VALID_AUDIO_SOURCES:
            raise ValueError("非法音频来源")
        if (
            type(through_chunk_seq) is not int
            or not 0 <= through_chunk_seq <= db.CHUNK_SEQ_MAX
        ):
            raise ValueError("非法音频取消水位")
        if reason not in db.VALID_AUDIO_CANCEL_REASONS:
            raise ValueError("非法音频取消原因")

        key = (session_id, source)
        self._begin_session_mutation(session_id)
        try:
            await self._reject_stopped_session(session_id)
            await run_db(db.ensure_recording, session_id)
            lock = self._audio_locks.setdefault(key, asyncio.Lock())
            async with lock:
                previous = self._audio_cancel_watermarks.get(key, -1)
                watermark = max(previous, through_chunk_seq)
                self._audio_cancel_watermarks[key] = watermark
                if through_chunk_seq >= previous:
                    self._audio_cancel_reasons[key] = (watermark, reason)

                current = self._audio_current.get(key)
                processing = self._audio_processing_tasks.get(key)
                if (
                    current is not None
                    and current.chunk_seq <= watermark
                    and processing is not None
                    and not processing.done()
                ):
                    processing.cancel()

                events = await run_db(
                    db.cancel_audio_source_chunks,
                    session_id,
                    source,
                    through_chunk_seq,
                    reason,
                )
                self._audio_next_seq[key] = max(
                    self._audio_next_seq.get(key, 0), watermark + 1
                )

                queue = self._audio_queues.get(key)
                pending = self._audio_pending.get(key)
                if queue is not None and pending is not None:
                    for chunk_seq in list(pending):
                        if chunk_seq <= watermark:
                            pending.pop(chunk_seq)
                            self._finish_audio_item(key, queue)

                if queue is not None:
                    preserved = []
                    while True:
                        try:
                            queued = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        queue.task_done()
                        if queued is _AUDIO_WAKE:
                            continue
                        if queued is None:
                            preserved.append(queued)
                            continue
                        if queued.chunk_seq <= watermark:
                            self._audio_outstanding[key] = max(
                                0, self._audio_outstanding.get(key, 0) - 1
                            )
                        else:
                            preserved.append(queued)
                    for queued in preserved:
                        queue.put_nowait(queued)
                    task = self._audio_tasks.get(key)
                    if task is not None and not task.done() and queue.empty():
                        queue.put_nowait(_AUDIO_WAKE)

            # 停止采集/切换来源意味着本轮语音段不会再有音频:立刻收段拿段末
            # final,否则这段文本要等到会话结束才落库,下一轮采集还会和它混成
            # 同一个 utterance。
            try:
                await self._finish_funasr_segment(key)
            except Exception:
                logger.exception(
                    "取消音频来源时刷新 FunASR 失败: session=%s source=%s",
                    session_id,
                    source,
                )
            await self._drop_funasr_stream(key)

            for event in events:
                await self.broadcast(session_id, event_message(event))
            return events
        finally:
            self._finish_session_mutation(session_id)

    async def enqueue_answer(self, item: AnswerWork) -> bool:
        self._begin_session_mutation(item.session_id)
        try:
            await self._reject_stopped_session(item.session_id)
            await run_db(db.ensure_recording, item.session_id)
            await self._reject_stopped_session(item.session_id)
            # 第三重屏障（对齐 enqueue_audio 的 reserve 后复查）：上面两次检查都
            # 让出过事件循环，end_session 的 stop_session 可能在任意一次 await 之后
            # 执行并 pop 掉 _answer_queues/_answer_tasks；若直接调 _answer_queue 会
            # 重建 queue+worker，worker 内 ensure_recording 抛错被吞后永久挂在
            # queue.get()。先复查再建队列。
            if item.session_id in self._stopped_sessions:
                session = await run_db(db.require_session, item.session_id)
                raise db.SessionStateError(session["status"], "recording")
            queue = self._answer_queue(item.session_id)
            if queue.full():
                return False
            queue.put_nowait(item)
            return True
        finally:
            self._finish_session_mutation(item.session_id)

    @staticmethod
    def _question_grace_seconds() -> float:
        try:
            return max(
                0.5,
                float(os.environ.get("AI_QUESTION_THREAD_GRACE_SECONDS", "6")),
            )
        except ValueError:
            return 6.0

    def _question_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        return self._question_locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _cancel_question_close(thread: QuestionThread) -> None:
        task = thread.close_task
        thread.close_task = None
        if task is not None and not task.done():
            task.cancel()

    @staticmethod
    def _question_ready_to_persist(thread: QuestionThread) -> bool:
        """线程关闭后，最新版成功即可落库；最新版失败则等旧版全部收尾。"""
        completion = thread.latest_completion
        return thread.closed and (
            (completion is not None and completion.revision == thread.revision)
            or not thread.pending_revisions
        )

    def _schedule_question_close_locked(
        self,
        key: tuple[str, str],
        thread: QuestionThread,
        deadline: float,
    ) -> None:
        self._cancel_question_close(thread)
        delay = max(0.05, deadline - asyncio.get_running_loop().time())
        thread.close_task = asyncio.create_task(
            self._close_question_after(key, thread.thread_id, delay),
            name=f"question-close:{thread.thread_id}",
        )

    async def _close_question_after(
        self, key: tuple[str, str], thread_id: str, delay: float
    ) -> None:
        await asyncio.sleep(delay)
        await self._close_question_thread(key, thread_id)

    async def mark_speech_end(
        self,
        session_id: str,
        source: str,
        through_chunk_seq: int,
    ) -> None:
        """记录客户端 VAD 边界；不阻塞 ASR，也不等待答案完成。"""
        await run_db(db.ensure_recording, session_id)
        key = (session_id, source)
        deadline = asyncio.get_running_loop().time() + self._question_grace_seconds()
        async with self._question_lock(key):
            previous = self._speech_end_boundaries.get(key)
            if previous is not None and previous[0] > through_chunk_seq:
                return
            self._speech_end_boundaries[key] = (through_chunk_seq, deadline)
            thread = self._question_threads.get(key)
            if thread is not None and not thread.closed:
                self._schedule_question_close_locked(key, thread, deadline)
        await self._close_funasr_segment_at(key, through_chunk_seq)

    async def _close_funasr_segment_at(
        self, key: tuple[str, str], through_chunk_seq: int
    ) -> None:
        """`speech_end` 到达时结束 FunASR 语音段(发 stop 拿段末 final)。

        边界分片可能还没被音频 worker 推给 FunASR;此时只记水位,由
        `_process_stream_chunk` 在推完该序号后收段。
        """
        if key not in self._funasr_streams:
            return
        segment = self._funasr_segments.get(key)
        if segment is None:
            return
        last = segment.last_chunk
        if last is not None and last.chunk_seq >= through_chunk_seq:
            await self._finish_funasr_segment(key)
            return
        self._funasr_pending_finish[key] = through_chunk_seq

    async def _enqueue_question_revision(
        self,
        key: tuple[str, str],
        cumulative_text: str,
        chunk_seq: int | None = None,
    ) -> bool:
        """把累计 partial 全文作为一次问题修订，立即并发提交给 LLM。

        `cumulative_text` 已经是当前语音段的全文(FunASR partial 语义),因此
        直接整段覆盖 `thread.question`,不再用 `join_transcript_text` 拼片。

        `chunk_seq` 是这一版文本所依据的最后一个分片序号,只用来判定上一个
        `speech_end` 边界是否已经过期(说话人在宽限期内又开口)。
        """
        async with self._question_lock(key):
            boundary = self._speech_end_boundaries.get(key)
            if (
                boundary is not None
                and chunk_seq is not None
                and chunk_seq > boundary[0]
            ):
                # 边界之后又来了新语音:说话人在宽限期内接着说,旧边界作废。
                # 取消关闭定时器,等下一个 speech_end 重新定界,否则这条追问会
                # 被上一个边界的宽限期提前截断。
                self._speech_end_boundaries.pop(key, None)
                boundary = None
            thread = self._question_threads.get(key)
            if thread is None or thread.closed:
                thread = QuestionThread(session_id=key[0], source=key[1])
                self._question_threads[key] = thread
                self._question_threads_by_id[thread.thread_id] = thread
            thread.question = join_transcript_text(
                thread.committed_prefix, cumulative_text
            )[: llm.MAX_QUESTION_CHARS]
            thread.revision += 1
            revision = thread.revision
            thread.pending_revisions.add(revision)
            if boundary is not None:
                self._schedule_question_close_locked(key, thread, boundary[1])
            else:
                self._cancel_question_close(thread)
            item = AnswerWork(
                session_id=key[0],
                question=thread.question,
                use_search=False,
                thread_id=thread.thread_id,
                revision=revision,
                persist_immediately=False,
            )
        try:
            accepted = await self.enqueue_answer(item)
        except Exception:
            await self._finish_question_revision(item.thread_id, revision, None)
            raise
        if not accepted:
            await self._finish_question_revision(item.thread_id, revision, None)
        return accepted

    async def regenerate_thread_answer(
        self,
        session_id: str,
        thread_id: str,
        question: str,
        use_search: bool,
    ) -> bool:
        """重问某张问题卡：revision+1，答案流回同一张卡。

        线程找不到时(后端重启过/极端清理)以相同 thread_id 重建,只进
        `_question_threads_by_id`——不进 `_question_threads` key 映射,不打断
        同 key 上可能正在进行的新问题。原线程已关闭的话,入队后补一次关闭,
        让重新生成的答案完成即落库(persist-on-finish)。
        """
        existing = self._question_threads_by_id.get(thread_id)
        if existing is not None and existing.session_id != session_id:
            # id 属于别的会话:不重建,避免跨会话串答案。
            return False
        rebuilt = existing is None
        if rebuilt:
            self._question_threads_by_id[thread_id] = QuestionThread(
                session_id=session_id, source="pc", thread_id=thread_id
            )
        key = (session_id, existing.source if existing is not None else "pc")
        item: AnswerWork | None = None
        revision = 0
        was_closed = False
        async with self._question_lock(key):
            thread = self._question_threads_by_id.get(thread_id)
            if thread is None:
                # 并发 regenerate 同卡,另一路刚把它清掉:罕见,放弃本次。
                return False
            # 重建的线程视作「原本已关闭」:它没有活跃语音段,生成完就该落库。
            was_closed = thread.closed or rebuilt
            had_persisted_answer = thread.persisted
            thread.closed = False
            thread.persisted = False
            if had_persisted_answer:
                # 旧答案已经在数据库里。重新生成若失败，不得把旧 completion
                # 再插入一遍形成重复历史记录。
                thread.latest_completion = None
            thread.question = question[: llm.MAX_QUESTION_CHARS]
            thread.revision += 1
            revision = thread.revision
            thread.pending_revisions.add(revision)
            self._cancel_question_close(thread)
            item = AnswerWork(
                session_id=session_id,
                question=thread.question,
                use_search=use_search,
                thread_id=thread_id,
                revision=revision,
                persist_immediately=False,
            )
        try:
            accepted = await self.enqueue_answer(item)
        except Exception:
            await self._finish_question_revision(thread_id, revision, None)
            raise
        if not accepted:
            await self._finish_question_revision(thread_id, revision, None)
        if was_closed and accepted:
            # 原线程早已过了宽限期:补一次关闭,让这次生成完成即落库。
            await self._close_question_thread(key, thread_id)
        return accepted

    async def _finish_question_revision(
        self,
        thread_id: str | None,
        revision: int,
        completion: AnswerCompletion | None,
    ) -> None:
        if thread_id is None:
            return
        thread = self._question_threads_by_id.get(thread_id)
        if thread is None:
            return
        key = (thread.session_id, thread.source)
        should_persist = False
        async with self._question_lock(key):
            thread = self._question_threads_by_id.get(thread_id)
            if thread is None:
                return
            thread.pending_revisions.discard(revision)
            if completion is not None and (
                thread.latest_completion is None
                or completion.revision >= thread.latest_completion.revision
            ):
                thread.latest_completion = completion
            should_persist = self._question_ready_to_persist(thread)
        if should_persist:
            await self._persist_question_thread(thread_id)

    async def _close_question_thread(
        self, key: tuple[str, str], thread_id: str
    ) -> None:
        should_persist = False
        async with self._question_lock(key):
            thread = self._question_threads_by_id.get(thread_id)
            if thread is None or thread.closed:
                return
            thread.closed = True
            thread.close_task = None
            if self._question_threads.get(key) is thread:
                self._question_threads.pop(key, None)
                # 只有关闭的是该 key 的**当前**线程才清 speech_end 边界:
                # regenerate 会把已关闭的旧线程重开再补关闭,若在这里无差别
                # pop,同 key 上正在说话的新线程的宽限边界会被误删,导致它
                # 的追问被上一个 speech_end 提前截断。
                self._speech_end_boundaries.pop(key, None)
            should_persist = self._question_ready_to_persist(thread)
        if should_persist:
            await self._persist_question_thread(thread_id)

    async def _persist_question_thread(self, thread_id: str) -> None:
        thread = self._question_threads_by_id.get(thread_id)
        if thread is None:
            return
        key = (thread.session_id, thread.source)
        async with self._question_lock(key):
            thread = self._question_threads_by_id.get(thread_id)
            if (
                thread is None
                or thread.persisted
                or thread.persisting
                or not self._question_ready_to_persist(thread)
            ):
                return
            completion = thread.latest_completion
            if completion is None:
                thread.persisted = True
                return
            thread.persisting = True
        persisted = False
        try:
            answer = await run_db(
                db.add_answer,
                thread.session_id,
                completion.question,
                completion.answer,
                completion.source,
                completion.request_id,
                thread.thread_id,
                completion.revision,
            )
            await self.broadcast(
                thread.session_id,
                server_message(
                    "answer",
                    event_id=answer["event_id"],
                    **{
                        field_name: value
                        for field_name, value in answer.items()
                        if field_name != "event_id"
                    },
                ),
            )
            persisted = True
        finally:
            async with self._question_lock(key):
                current = self._question_threads_by_id.get(thread_id)
                if current is not None:
                    current.persisting = False
                    if persisted:
                        current.persisted = True

    async def _close_session_question_threads(self, session_id: str) -> None:
        targets = [
            (key, thread.thread_id)
            for key, thread in self._question_threads.items()
            if key[0] == session_id
        ]
        for key, thread_id in targets:
            await self._close_question_thread(key, thread_id)

    async def _broadcast_chunk_event(self, session_id: str, event: dict | None) -> None:
        if event:
            await self.broadcast(session_id, event_message(event))

    async def _persist_single_final(
        self, key: tuple[str, str], current: AudioWork, text: str
    ) -> None:
        text = normalize_segment_text(self._transcript_tails.get(key, ""), text)
        if not text:
            event = await run_db(
                db.mark_audio_chunk_status, current.chunk_id, "done"
            )
            await self._broadcast_chunk_event(current.session_id, event)
            return
        transcript = await run_db(
            db.add_transcript,
            current.session_id,
            current.source,
            text,
            chunk_id=current.chunk_id,
            chunk_seq=current.chunk_seq,
            captured_at=current.captured_at.isoformat(),
        )
        self._transcript_tails[key] = text[-_LAST_TAIL_CHARS:]
        chunk_event = transcript.pop("chunk_event", None)
        transcript_event_id = transcript.pop("event_id")
        await self.broadcast(
            current.session_id,
            server_message(
                "transcript", event_id=transcript_event_id, **transcript
            ),
        )
        await self._broadcast_chunk_event(current.session_id, chunk_event)
        # 非流式引擎(Groq)没有累计 partial:每片自己是一个 final。与流式路径
        # 同一契约——只传本片文本,committed_prefix 由 _enqueue_question_revision
        # 内部拼接且只拼一次;随后把全文固化给下一片,否则前缀会随分片数翻倍。
        accepted = await self._enqueue_question_revision(
            key, text, current.chunk_seq
        )
        await self._commit_question_prefix(key)
        if not accepted:
            await self.broadcast(
                current.session_id,
                server_message(
                    "error",
                    code="answer_backpressure",
                    message="答案生成队列已满，请稍后重试",
                ),
            )

    def _funasr_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        return self._funasr_locks.setdefault(key, asyncio.Lock())

    async def _persist_segment_final(
        self, key: tuple[str, str], segment: FunAsrSegment, final_text: str
    ) -> None:
        """段末 final 入库为**一条** transcript,绑定到该语音段的最后一片。

        分片在推给 FunASR 时就已经 ack 成 done(见 `_ack_chunk_done`),所以这里
        不再逐片改状态;`add_transcript` 需要一个 queued 分片才能绑定,因此段末
        直接走无 chunk_id 的写入路径。
        """
        session_id, source = key
        final_text = normalize_segment_text(
            self._transcript_tails.get(key, ""), final_text
        )
        if not final_text:
            return
        last = segment.last_chunk
        transcript = await run_db(
            db.add_transcript,
            session_id,
            source,
            final_text,
            captured_at=(
                last.captured_at.isoformat() if last is not None else None
            ),
        )
        self._transcript_tails[key] = final_text[-_LAST_TAIL_CHARS:]
        transcript.pop("chunk_event", None)
        transcript_event_id = transcript.pop("event_id")
        await self.broadcast(
            session_id,
            server_message("transcript", event_id=transcript_event_id, **transcript),
        )

    async def _ack_chunk_done(self, current: AudioWork) -> None:
        """分片一旦推进 FunASR 就立刻 ack done。

        开放式 utterance 下一个问题可能有十几片,若等段末 final 才 ack,客户端
        outbox 的 8 槽背压会把采集冻住。转写文本改由段末单条 transcript 承载,
        不再和具体分片绑定。
        """
        event = await run_db(db.mark_audio_chunk_status, current.chunk_id, "done")
        await self._broadcast_chunk_event(current.session_id, event)

    async def _process_stream_events(
        self,
        key: tuple[str, str],
        segment: FunAsrSegment,
        events: list[asr.FunAsrEvent],
    ) -> None:
        """把 FunASR 事件转成前端显示 + LLM revision。

        `partial` 是当前语音段的**累计全文**,所以直接整段替换显示,并整段作为
        新 revision 的问题提交 LLM——不需要拼接分片文本。段内每来一版有效增量
        就开一次并发 LLM 请求,实现"先问一部分,不等说完"。
        """
        session_id, source = key
        latest_partial: str | None = None
        final_text: str | None = None
        for event in events:
            if event.type == "partial":
                latest_partial = event.text
            elif event.type == "final":
                final_text = event.text
        if latest_partial is not None and final_text is None:
            await self.broadcast(
                session_id,
                server_message(
                    "transcript_partial",
                    session_id=session_id,
                    source=source,
                    text=latest_partial,
                ),
            )
            await self._dispatch_segment_revision(key, segment, latest_partial)
        if final_text is not None:
            # final 即使是空串也要让客户端清掉上一条临时转写。
            await self.broadcast(
                session_id,
                server_message(
                    "transcript_partial",
                    session_id=session_id,
                    source=source,
                    text="",
                ),
            )
            await self._dispatch_segment_revision(key, segment, final_text, force=True)
            await self._commit_question_prefix(key)
            await self._persist_segment_final(key, segment, final_text)

    async def _commit_question_prefix(self, key: tuple[str, str]) -> None:
        """语音段结束时固化问题前缀。

        宽限期内说话人继续说会开启新语音段;新段的累计 partial 只覆盖新段,必须
        拼在已固化的前缀后面,否则第二段会把第一段的完整问题覆盖掉。
        """
        async with self._question_lock(key):
            thread = self._question_threads.get(key)
            if thread is not None and not thread.closed:
                thread.committed_prefix = thread.question

    async def _dispatch_segment_revision(
        self,
        key: tuple[str, str],
        segment: FunAsrSegment,
        text: str,
        *,
        force: bool = False,
    ) -> None:
        """按累计全文开一个新 revision;增长不足则跳过,避免同一句刷多次 LLM。

        节流规则(仅在文本是上一版的纯追加时生效,即 FunASR 累计 partial 的常态):
        字符数要增长到上一版的 `_revision_growth_ratio()` 倍,或者追加部分出现
        句末标点。第一版(previous 为空)立即发;ASR 改稿(不以旧文开头)立即发;
        `force=True`(段末 final)总是发,保证入库答案对应段末固化文本。
        """
        text = text.strip()
        if not text:
            return
        previous = segment.dispatched_text
        if text == previous:
            return
        if not force and previous and text.startswith(previous):
            appended = text[len(previous):]
            if (
                len(text) < len(previous) * _revision_growth_ratio()
                and not _SENTENCE_END.search(appended)
            ):
                return
        segment.dispatched_text = text
        last = segment.last_chunk
        if not await self._enqueue_question_revision(
            key, text, last.chunk_seq if last is not None else None
        ):
            await self.broadcast(
                key[0],
                server_message(
                    "error",
                    code="answer_backpressure",
                    message="答案生成队列已满，请稍后重试",
                ),
            )

    async def _fail_segment_chunks(
        self, key: tuple[str, str], error_code: str = "processing_failed"
    ) -> None:
        segment = self._funasr_segments.get(key)
        if segment is None:
            return
        for item in segment.chunks:
            event = await run_db(
                db.mark_audio_chunk_status,
                item.chunk_id,
                "failed",
                error_code=error_code,
            )
            await self._broadcast_chunk_event(key[0], event)

    async def _finish_funasr_segment(self, key: tuple[str, str]) -> None:
        """结束当前语音段:发 stop、取段末 final、落库一条 transcript。

        连接本身保留给下一个语音段复用,只有 `_drop_funasr_stream` 才关连接。
        """
        async with self._funasr_lock(key):
            self._funasr_pending_finish.pop(key, None)
            stream = self._funasr_streams.get(key)
            segment = self._funasr_segments.pop(key, None)
            if stream is None or segment is None:
                return
            try:
                events = await stream.finish()
                # 某些部署在 stop 后只回最后一次 partial；在明确结束语音段时，
                # 将它作为本段最终文本，避免最后一句丢失。
                if not any(event.type == "final" for event in events):
                    partials = [event for event in events if event.type == "partial"]
                    text = partials[-1].text if partials else segment.dispatched_text
                    events.append(asr.FunAsrEvent("final", text=text))
                await self._process_stream_events(key, segment, events)
            except Exception:
                self._funasr_segments[key] = segment
                await self._fail_segment_chunks(key)
                self._funasr_segments.pop(key, None)
                await self._drop_funasr_stream(key)
                raise

    async def _drop_funasr_stream(self, key: tuple[str, str]) -> None:
        stream = self._funasr_streams.pop(key, None)
        self._funasr_segments.pop(key, None)
        self._funasr_pending_finish.pop(key, None)
        if stream is not None:
            await stream.close()

    async def _process_audio_work(
        self, key: tuple[str, str], current: AudioWork
    ) -> None:
        if asr.use_funasr_stream(current.codec):
            await self._process_stream_chunk(key, current)
            return

        text = await asr.transcribe_audio(
            current.audio_bytes,
            current.codec,
            current.source,
            current.duration_ms,
        )
        await self._persist_single_final(key, current, text)

    async def _process_stream_chunk(
        self, key: tuple[str, str], current: AudioWork
    ) -> None:
        async with self._funasr_lock(key):
            stream = self._funasr_streams.get(key)
            if stream is None:
                stream = asr.FunAsrStream()
                await stream.connect()
                self._funasr_streams[key] = stream
            segment = self._funasr_segments.get(key)
            if segment is None:
                segment = FunAsrSegment(session_id=key[0], source=key[1])
                self._funasr_segments[key] = segment
            segment.chunks.append(current)
            segment.last_chunk = current
            try:
                events = await stream.push_wav(current.audio_bytes, current.duration_ms)
            except Exception:
                await self._fail_segment_chunks(key)
                await self._drop_funasr_stream(key)
                raise
            # 推流成功即 ack：一个问题可能十几片，等段末 final 会触发客户端背压。
            await self._ack_chunk_done(current)
            segment.chunks.clear()
            await self._process_stream_events(key, segment, events)
        boundary = self._funasr_pending_finish.get(key)
        if boundary is not None and current.chunk_seq >= boundary:
            # speech_end 先于边界分片到达：分片补齐后立刻收段，不必等宽限期。
            await self._finish_funasr_segment(key)

    async def _audio_worker(
        self, key: tuple[str, str], queue: asyncio.Queue[object]
    ) -> None:
        pending: dict[int, AudioWork] = {}
        self._audio_pending[key] = pending
        expected_seq = self._audio_next_seq.get(key)
        if expected_seq is None:
            expected_seq = await run_db(db.get_next_audio_chunk_seq, key[0], key[1])
            self._audio_next_seq[key] = expected_seq
        expected_seq = max(
            expected_seq, self._audio_cancel_watermarks.get(key, -1) + 1
        )
        resume_expected_seq: int | None = None
        current: AudioWork | None = None
        try:
            while True:
                expected_seq = max(
                    expected_seq, self._audio_cancel_watermarks.get(key, -1) + 1
                )
                if expected_seq not in pending:
                    try:
                        if pending:
                            wait_seconds = float(
                                os.environ.get("AI_AUDIO_REORDER_WAIT_SECONDS", "5")
                            )
                            received = await asyncio.wait_for(
                                queue.get(), timeout=max(0.1, wait_seconds)
                            )
                        else:
                            received = await queue.get()
                    except asyncio.TimeoutError:
                        # 同一洞的连续超时计数:首次报缺口并回洞头等补传(客户端可能重发);
                        # 连续第二次仍缺 → 判定洞被放弃,静默跳过(防止错误永动机刷屏)
                        gap_counts = self._gap_timeouts.get(key, {})
                        count = gap_counts.get(expected_seq, 0) + 1
                        gap_counts[expected_seq] = count
                        self._gap_timeouts[key] = gap_counts
                        abandoned = count >= 2
                        lock = self._audio_locks.setdefault(key, asyncio.Lock())
                        async with lock:
                            candidates = list(pending.values())
                            pending.clear()
                            held_back = []
                            for item in candidates:
                                queue.task_done()
                                if abandoned and not self._audio_is_cancelled(
                                    key, item.chunk_seq
                                ):
                                    # queue.get 已增加 unfinished 计数；重放前先
                                    # task_done 再 put，使 unfinished/outstanding 净值不变。
                                    held_back.append(item)
                                    queue.put_nowait(item)
                                else:
                                    self._audio_outstanding[key] = max(
                                        0,
                                        self._audio_outstanding.get(key, 0) - 1,
                                    )
                            if held_back:
                                expected_seq = max(
                                    min(item.chunk_seq for item in held_back),
                                    self._audio_cancel_watermarks.get(key, -1) + 1,
                                )
                                self._audio_next_seq[key] = expected_seq

                        if not abandoned:
                            for item in candidates:
                                if self._audio_is_cancelled(key, item.chunk_seq):
                                    continue
                                event = await run_db(
                                    db.mark_audio_chunk_status,
                                    item.chunk_id,
                                    "failed",
                                    error_code="missing_predecessor",
                                )
                                if not event:
                                    continue
                                await self.broadcast(
                                    item.session_id, event_message(event)
                                )
                                await self.broadcast(
                                    item.session_id,
                                    server_message(
                                        "error",
                                        code="audio_sequence_gap",
                                        message="音频序号存在缺口，等待补传缺失分片",
                                        chunk_id=item.chunk_id,
                                        chunk_seq=item.chunk_seq,
                                        expected_chunk_seq=expected_seq,
                                    ),
                                )
                        continue
                    if received is _AUDIO_WAKE:
                        queue.task_done()
                        continue
                    if self._audio_is_cancelled(key, received.chunk_seq):
                        self._finish_audio_item(key, queue)
                        continue
                    if received.chunk_seq < expected_seq:
                        # 迟到的重传(seq 已被越过):前序早已终态化,
                        # 就地处理这一片再恢复原序列,不进 pending 死等
                        pending[received.chunk_seq] = received
                        resume_expected_seq = expected_seq
                        expected_seq = received.chunk_seq
                        continue
                    pending[received.chunk_seq] = received
                    continue

                current = pending.pop(expected_seq)
                self._audio_current[key] = current
                advance_sequence = False
                try:
                    if self._audio_is_cancelled(key, current.chunk_seq):
                        raise _AudioSourceCancelled
                    await run_db(db.ensure_recording, current.session_id)
                    if self._audio_is_cancelled(key, current.chunk_seq):
                        raise _AudioSourceCancelled
                    processing = asyncio.create_task(
                        self._process_audio_work(key, current),
                        name=(
                            f"asr:{current.session_id}:{current.source}:"
                            f"{current.chunk_seq}"
                        ),
                    )
                    self._audio_processing_tasks[key] = processing
                    try:
                        await processing
                    finally:
                        if self._audio_processing_tasks.get(key) is processing:
                            self._audio_processing_tasks.pop(key, None)
                    if self._audio_is_cancelled(key, current.chunk_seq):
                        raise _AudioSourceCancelled
                    advance_sequence = True
                except _AudioSourceCancelled:
                    advance_sequence = True
                except db.AudioSourceNotAllowedError:
                    events = await run_db(
                        db.cancel_audio_source_chunks,
                        current.session_id,
                        current.source,
                        current.chunk_seq,
                        "source_disabled",
                    )
                    for event in events:
                        await self.broadcast(
                            current.session_id, event_message(event)
                        )
                    advance_sequence = True
                except db.SessionStateError:
                    if current:
                        event = await run_db(
                            db.mark_audio_chunk_status,
                            current.chunk_id,
                            "cancelled",
                            error_code="session_not_recording",
                        )
                        if event:
                            await self.broadcast(
                                current.session_id, event_message(event)
                            )
                except (db.UsageLimitExceeded, cost_control.PaidCallBusyError) as exc:
                    event = await run_db(
                        db.mark_audio_chunk_status,
                        current.chunk_id,
                        "failed",
                        error_code="usage_limited",
                    )
                    if event:
                        await self.broadcast(current.session_id, event_message(event))
                    retry_after = getattr(exc, "retry_after_seconds", 1)
                    await self.broadcast(
                        current.session_id,
                        server_message(
                            "error",
                            code="paid_usage_limited",
                            message="付费服务预算或并发已达上限，请稍后重试",
                            chunk_id=current.chunk_id,
                            retry_after_seconds=retry_after,
                        ),
                    )
                except asyncio.CancelledError:
                    if self._audio_is_cancelled(key, current.chunk_seq):
                        advance_sequence = True
                    else:
                        reason = self._stop_reasons.get(
                            current.session_id, "service_shutdown"
                        )
                        status = (
                            "failed" if reason == "service_shutdown" else "cancelled"
                        )
                        await run_db(
                            db.mark_audio_chunk_status,
                            current.chunk_id,
                            status,
                            error_code=reason,
                        )
                        raise
                except Exception as exc:
                    logger.exception(
                        "音频分片处理失败: session=%s chunk=%s",
                        current.session_id,
                        current.chunk_id,
                    )
                    # 配置类错误(未配置/鉴权失败):原因直达客户端,不可重试且推进序号,
                    # 否则每个静音分片都会重复失败并触发序号缺口风暴。
                    # 优先按结构化异常分类;字符串匹配仅兜底旧第三方异常
                    # (如旧版 httpx 错误消息),FunASR/Groq 均已抛结构化类型。
                    is_config_error = _is_config_error(exc)
                    error_code = "config_missing" if is_config_error else "processing_failed"
                    event = await run_db(
                        db.mark_audio_chunk_status,
                        current.chunk_id,
                        "failed",
                        error_code=error_code,
                    )
                    if event:
                        await self.broadcast(current.session_id, event_message(event))
                    await self.broadcast(
                        current.session_id,
                        server_message(
                            "error",
                            code="audio_processing_failed",
                            message=(
                                _audio_config_error_message()
                                if is_config_error
                                else _audio_runtime_error_message()
                            ),
                            chunk_id=current.chunk_id,
                        ),
                    )
                    if is_config_error:
                        advance_sequence = True
                    elif error_code == "processing_failed":
                        # 转写服务当下失败(如网络不通):也推进序列,否则后续分片
                        # 全部卡成 missing_predecessor 连环错;重试由客户端 outbox/对账兜底
                        advance_sequence = True
                finally:
                    self._finish_audio_item(key, queue)
                    if self._audio_current.get(key) is current:
                        self._audio_current.pop(key, None)
                    current = None
                if advance_sequence:
                    expected_seq += 1
                    if resume_expected_seq is not None:
                        expected_seq = max(expected_seq, resume_expected_seq)
                        resume_expected_seq = None
                    # 重启恢复时，当前重试分片之后可能已经存在 done/cancelled
                    # （或不可重试 failed）的连续终态。只做 +1 会把这些已消费
                    # 序号再次当成缺口，导致下一片被误标 missing_predecessor。
                    expected_seq = await run_db(
                        db.get_next_audio_chunk_seq,
                        key[0],
                        key[1],
                        expected_seq,
                    )
                    expected_seq = max(
                        expected_seq,
                        self._audio_cancel_watermarks.get(key, -1) + 1,
                    )
                    self._audio_next_seq[key] = expected_seq
                    # 序号正常前进:清掉该序号的缺口计数(洞已补上或已越过)
                    counts = self._gap_timeouts.get(key)
                    if counts:
                        counts.pop(expected_seq - 1, None)
                        counts.pop(expected_seq, None)
        finally:
            for item in pending.values():
                self._finish_audio_item(key, queue)
            pending.clear()
            while True:
                try:
                    queued = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                queue.task_done()
                if queued is not None and queued is not _AUDIO_WAKE:
                    self._audio_outstanding[key] = max(
                        0, self._audio_outstanding.get(key, 0) - 1
                    )
            self._audio_queues.pop(key, None)
            self._audio_tasks.pop(key, None)
            self._audio_pending.pop(key, None)
            self._audio_current.pop(key, None)
            self._audio_processing_tasks.pop(key, None)
            if self._audio_outstanding.get(key, 0) <= 0:
                self._audio_outstanding.pop(key, None)

    async def _answer_worker(
        self, session_id: str, queue: asyncio.Queue[AnswerWork]
    ) -> None:
        active = self._answer_generation_tasks.setdefault(session_id, set())
        try:
            while True:
                completed = {task for task in active if task.done()}
                if completed:
                    await asyncio.gather(*completed, return_exceptions=True)
                    active.difference_update(completed)
                if len(active) >= self.answer_concurrency:
                    completed, _ = await asyncio.wait(
                        active, return_when=asyncio.FIRST_COMPLETED
                    )
                    await asyncio.gather(*completed, return_exceptions=True)
                    active.difference_update(completed)
                    continue
                item = await queue.get()
                generation = asyncio.create_task(
                    self._run_answer_item(session_id, item, queue),
                    name=f"answer-generation:{session_id}:{item.request_id}",
                )
                active.add(generation)
        finally:
            active = self._answer_generation_tasks.pop(session_id, set())
            for generation in active:
                if not generation.done():
                    generation.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                queue.task_done()
            self._answer_queues.pop(session_id, None)
            self._answer_tasks.pop(session_id, None)

    async def _run_answer_item(
        self,
        session_id: str,
        item: AnswerWork,
        queue: asyncio.Queue[AnswerWork],
    ) -> None:
        cancelled = False
        try:
            await self._generate_answer(session_id, item)
        except asyncio.CancelledError:
            # 取消时保留 capture:flush_session 兜底落部分答案后自行清理。
            cancelled = True
            raise
        finally:
            queue.task_done()
            if not cancelled:
                self._answer_stream_captures.pop(item.request_id, None)

    async def _generate_answer(self, session_id: str, item: AnswerWork) -> None:
        revision_finished = False
        answer_text = ""
        source = "llm"

        async def emit_stream_frame(
            *, delta: str, answer: str, started: bool, done: bool, failed: bool
        ) -> None:
            # token 帧只带 delta（answer 恒为空串），终止帧（done/failed）才携带
            # 全文一次；twin 字段 text 已从协议删除，全文字段统一为 answer。
            await self.broadcast(
                session_id,
                server_message(
                    "answer_stream",
                    session_id=session_id,
                    request_id=item.request_id,
                    thread_id=item.thread_id,
                    revision=item.revision,
                    question=item.question,
                    channel="answer",
                    delta=delta,
                    answer=answer,
                    source=source,
                    started=started,
                    done=done,
                    failed=failed,
                ),
            )

        try:
            await run_db(db.ensure_recording, session_id)
            context = await run_db(db.get_recent_transcript_context, session_id)
            # 岗位 JD 与简历让答案贴合这个岗位和这份履历；缺省时退化为通用答案。
            job_description, resume = await run_db(db.get_session_context, session_id)
            if item.thread_id is not None:
                await emit_stream_frame(
                    delta="", answer="", started=True, done=False, failed=False
                )
            if item.use_search:
                stream = llm.stream_answer_with_search_info(
                    item.question, context, job_description, resume
                )
            elif item.image_bytes is not None:
                # 笔试辅助：截图直接进多模态，不带转写上下文。笔试题面和
                # 面试对话没有关系，塞进去只会稀释题目并多烧 token。
                stream = self._screenshot_answer_stream(
                    item.image_bytes,
                    item.image_mime,
                    item.question,
                    job_description,
                    resume,
                )
            else:
                stream = self._plain_answer_stream(
                    item.question, context, job_description, resume
                )
            capture = _AnswerStreamCapture(item=item)
            self._answer_stream_captures[item.request_id] = capture
            async for part, used_search in stream:
                source = "search+llm" if used_search else "llm"
                # 思考过程功能已下线:上游 reasoning 增量只用于跳过空 delta,不再
                # 广播 channel="thinking",也不随 answer 事件下发。
                if part.text:
                    answer_text += part.text
                    capture.text = answer_text
                    capture.source = source
                    await emit_stream_frame(
                        delta=part.text,
                        answer="",
                        started=False,
                        done=False,
                        failed=False,
                    )
            if not answer_text.strip():
                raise RuntimeError("LLM 返回了空答案")
            await emit_stream_frame(
                delta="",
                answer=answer_text,
                started=False,
                done=True,
                failed=False,
            )
            if item.persist_immediately:
                answer = await run_db(
                    db.add_answer,
                    session_id,
                    item.question,
                    answer_text,
                    source,
                    item.request_id,
                )
                await self.broadcast(
                    session_id,
                    server_message(
                        "answer",
                        event_id=answer["event_id"],
                        **{
                            key: value
                            for key, value in answer.items()
                            if key != "event_id"
                        },
                        ),
                )
            else:
                await self._finish_question_revision(
                    item.thread_id,
                    item.revision,
                    AnswerCompletion(
                        request_id=item.request_id,
                        revision=item.revision,
                        question=item.question,
                        answer=answer_text,
                        source=source,
                    ),
                )
                revision_finished = True
        except db.SessionStateError:
            if not item.persist_immediately and not revision_finished:
                await self._finish_question_revision(
                    item.thread_id, item.revision, None
                )
        except (db.UsageLimitExceeded, cost_control.PaidCallBusyError) as exc:
            if not item.persist_immediately and not revision_finished:
                await self._finish_question_revision(
                    item.thread_id, item.revision, None
                )
                revision_finished = True
                await emit_stream_frame(
                    delta="",
                    answer=answer_text,
                    started=False,
                    done=True,
                    failed=True,
                )
                await self.broadcast(
                    session_id,
                    server_message(
                        "error",
                        code="paid_usage_limited",
                        message="付费服务预算或并发已达上限，请稍后重试",
                        retry_after_seconds=getattr(exc, "retry_after_seconds", 1),
                    ),
                )
        except asyncio.CancelledError:
            if not item.persist_immediately and not revision_finished:
                # 会话结束/服务关闭会取消任务。清理放独立任务，确保本任务
                # 立即 unwind 并释放付费并发槽。
                asyncio.create_task(
                    self._cleanup_cancelled_revision(item, session_id)
                )
            raise
        except Exception:
            if not item.persist_immediately and not revision_finished:
                await self._finish_question_revision(
                    item.thread_id, item.revision, None
                )
                revision_finished = True
                await emit_stream_frame(
                    delta="",
                    answer=answer_text,
                    started=False,
                    done=True,
                    failed=True,
                )
                logger.exception("答案生成失败: session=%s", session_id)
                await self.broadcast(
                    session_id,
                    server_message(
                        "error",
                        code="answer_generation_failed",
                        message="答案生成失败，请稍后重试",
                    ),
                )

    async def _cleanup_cancelled_revision(
        self,
        item: AnswerWork,
        session_id: str,
    ) -> None:
        """被会话停止取消的线程 revision 收尾，清理 pending 记账。

        从 CancelledError 处理器 spawn 出来跑——取消的任务里再 await 会被
        立即打断,清理必须发生在独立任务里。
        """
        try:
            await self._finish_question_revision(item.thread_id, item.revision, None)
        except Exception:
            logger.exception(
                "取消清理失败: session=%s thread=%s revision=%s",
                session_id,
                item.thread_id,
                item.revision,
            )
            return

    @staticmethod
    async def _plain_answer_stream(
        question: str,
        context: str,
        job_description: str = "",
        resume: str = "",
    ):
        async for part in llm.stream_answer(
            question, context, job_description, resume
        ):
            yield part, False

    @staticmethod
    async def _screenshot_answer_stream(
        image_bytes: bytes,
        image_mime: str,
        note: str,
        job_description: str = "",
        resume: str = "",
    ):
        """截图解题也走 (part, used_search) 形状，好让上层广播逻辑只有一份。"""
        async for part in llm.stream_solve_screenshot(
            image_bytes, image_mime, note, job_description, resume
        ):
            yield part, False

    @staticmethod
    def _final_flush_timeout_seconds() -> float:
        try:
            return float(
                os.environ.get("AI_FINAL_ANSWER_FLUSH_TIMEOUT_SECONDS", "10")
            )
        except ValueError:
            return 10.0

    async def flush_session(self, session_id: str) -> None:
        """结束会话前结束 FunASR 语音段，确保最后的 partial 不丢失。"""
        timeout = self._final_flush_timeout_seconds()
        processing = [
            task
            for key, task in self._audio_processing_tasks.items()
            if key[0] == session_id and not task.done()
        ]
        if processing:
            # FunASR TCP 卡死时转写任务永不返回:等待必须有界,超时留给
            # stop_session 取消,否则 end_session 会挂死。
            try:
                await asyncio.wait_for(
                    asyncio.gather(*processing, return_exceptions=True),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "结束会话时等待音频转写超时: session=%s", session_id
                )
        for key in [key for key in self._funasr_streams if key[0] == session_id]:
            try:
                await self._finish_funasr_segment(key)
            except Exception:
                logger.exception("结束会话时刷新 FunASR 失败: session=%s", session_id)
            finally:
                await self._drop_funasr_stream(key)
        queue = self._answer_queues.get(session_id)
        if queue is not None:
            try:
                await asyncio.wait_for(queue.join(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("结束会话时等待最终答案超时: session=%s", session_id)
        await self._wait_for_final_answers(session_id)
        await self._close_session_question_threads(session_id)

    async def _wait_for_final_answers(self, session_id: str) -> None:
        """以 LLM 流超时为上限等在途答案生成收尾,让最终答案赶在会话结束前落库。

        `queue.join()` 只等 flush 超时(默认 10s),而 LLM 流自身可跑到 60s;超时后
        直接取消会把已消耗预算的答案整段丢掉。这里继续等在途生成(通常毫秒级,
        绝大多数流早已完成);等待期间 worker 从积压队列新开的生成也收编在同一
        上限内。到上限仍没完的,取消后把已累计的部分答案落库。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + llm.STREAM_TIMEOUT_SECONDS
        while True:
            pending = [
                task
                for task in self._answer_generation_tasks.get(session_id, ())
                if not task.done()
            ]
            if not pending:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                await self._persist_partial_answers(session_id)
                return
            await asyncio.wait(pending, timeout=remaining)

    async def _persist_partial_answers(self, session_id: str) -> None:
        """取消在途生成后,把已累计的部分答案落库,避免已消耗预算的答案丢失。

        同一问题线程只落 revision 最高且已有内容的那一版(与线程正常落库取
        最新成功版的语义一致)——最高 revision 在首 delta 前被取消时,退回
        次高的非空版;无线程的即时问答各自落一条。
        """
        captures = []
        for request_id, capture in list(self._answer_stream_captures.items()):
            if capture.item.session_id == session_id:
                self._answer_stream_captures.pop(request_id, None)
                captures.append(capture)
        best: dict[str, _AnswerStreamCapture] = {}
        for capture in captures:
            if not capture.text.strip():
                continue
            key = capture.item.thread_id or f"request:{capture.item.request_id}"
            current = best.get(key)
            if current is None or capture.item.revision > current.item.revision:
                best[key] = capture
        for capture in best.values():
            try:
                answer = await run_db(
                    db.add_answer,
                    session_id,
                    capture.item.question,
                    capture.text,
                    capture.source,
                    capture.item.request_id,
                    capture.item.thread_id,
                    capture.item.revision,
                )
            except Exception:
                logger.exception(
                    "落库部分答案失败: session=%s request=%s",
                    session_id,
                    capture.item.request_id,
                )
                continue
            await self.broadcast(
                session_id,
                server_message(
                    "answer",
                    event_id=answer["event_id"],
                    **{
                        field_name: value
                        for field_name, value in answer.items()
                        if field_name != "event_id"
                    },
                ),
            )

    async def stop_session(self, session_id: str) -> None:
        """结束会话时取消在途任务并清空队列，保证 ended 后不再写入。"""
        self._stopped_sessions.add(session_id)
        self._stop_reasons[session_id] = "session_ended"
        tasks = []
        for key, task in list(self._audio_tasks.items()):
            if key[0] == session_id:
                task.cancel()
                tasks.append(task)
        for key, task in list(self._audio_processing_tasks.items()):
            # worker 被 cancel 后其内部 await 的转写任务不会跟着取消,必须一并
            # cancel,否则卡死的 FunASR 转写任务会泄漏到进程退出。
            if key[0] == session_id and not task.done():
                task.cancel()
                tasks.append(task)
        answer_task = self._answer_tasks.get(session_id)
        if answer_task:
            answer_task.cancel()
            tasks.append(answer_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for key in [key for key in self._funasr_streams if key[0] == session_id]:
            await self._drop_funasr_stream(key)
            self._funasr_locks.pop(key, None)
        for key, thread in list(self._question_threads.items()):
            if key[0] != session_id:
                continue
            self._cancel_question_close(thread)
            self._question_threads.pop(key, None)
            self._question_threads_by_id.pop(thread.thread_id, None)
            self._question_locks.pop(key, None)
            self._speech_end_boundaries.pop(key, None)
        # 已入库的线程现在也保留在 _question_threads_by_id 里(供 regenerate
        # 重开),会话结束必须一并清掉,否则整场面试的线程对象会留到进程退出。
        for thread_id, thread in list(self._question_threads_by_id.items()):
            if thread.session_id == session_id:
                self._question_threads_by_id.pop(thread_id, None)
        await run_db(db.cancel_pending_audio_chunks, session_id, "session_ended")
        self._stop_reasons.pop(session_id, None)
        self._release_stopped_session_if_idle(session_id)
        self._answer_queues.pop(session_id, None)
        self._answer_tasks.pop(session_id, None)
        for request_id, capture in list(self._answer_stream_captures.items()):
            if capture.item.session_id == session_id:
                self._answer_stream_captures.pop(request_id, None)
        audio_keys = {
            key
            for mapping in (
                self._audio_queues,
                self._audio_tasks,
                self._audio_locks,
                self._audio_outstanding,
                self._audio_next_seq,
                self._audio_pending,
                self._audio_current,
                self._audio_processing_tasks,
                self._audio_cancel_watermarks,
                self._audio_cancel_reasons,
                self._gap_timeouts,
                self._transcript_tails,
            )
            for key in mapping
            if key[0] == session_id
        }
        for key in audio_keys:
            self._audio_queues.pop(key, None)
            self._audio_tasks.pop(key, None)
            self._audio_locks.pop(key, None)
            self._audio_outstanding.pop(key, None)
            self._audio_next_seq.pop(key, None)
            self._audio_pending.pop(key, None)
            self._audio_current.pop(key, None)
            self._audio_processing_tasks.pop(key, None)
            self._audio_cancel_watermarks.pop(key, None)
            self._audio_cancel_reasons.pop(key, None)
            self._gap_timeouts.pop(key, None)
            self._transcript_tails.pop(key, None)

    async def shutdown(self) -> None:
        session_ids = {
            *[key[0] for key in self._audio_tasks],
            *self._answer_tasks.keys(),
        }
        current_loop = asyncio.get_running_loop()
        tasks = [
            *self._audio_tasks.values(),
            *[
                task
                for task in self._audio_processing_tasks.values()
                if not task.done()
            ],
            *self._answer_tasks.values(),
        ]
        current_tasks = [task for task in tasks if task.get_loop() is current_loop]
        for session_id in session_ids:
            self._stop_reasons[session_id] = "service_shutdown"
        for task in current_tasks:
            task.cancel()
        if current_tasks:
            await asyncio.gather(*current_tasks, return_exceptions=True)
        for session_id in session_ids:
            await run_db(db.cancel_pending_audio_chunks, session_id, "service_shutdown")
        for stream in list(self._funasr_streams.values()):
            await stream.close()
        for thread in self._question_threads_by_id.values():
            self._cancel_question_close(thread)
        self._funasr_streams.clear()
        self._funasr_segments.clear()
        self._funasr_pending_finish.clear()
        self._funasr_locks.clear()
        self._question_threads.clear()
        self._question_threads_by_id.clear()
        self._question_locks.clear()
        self._speech_end_boundaries.clear()
        self._audio_queues.clear()
        self._audio_tasks.clear()
        self._audio_locks.clear()
        self._audio_outstanding.clear()
        self._audio_next_seq.clear()
        self._audio_pending.clear()
        self._audio_current.clear()
        self._audio_processing_tasks.clear()
        self._audio_cancel_watermarks.clear()
        self._audio_cancel_reasons.clear()
        self._transcript_tails.clear()
        self._answer_queues.clear()
        self._answer_tasks.clear()
        self._answer_generation_tasks.clear()
        self._answer_stream_captures.clear()
        self._stopped_sessions.clear()
        self._stop_reasons.clear()
        self._session_mutations.clear()
