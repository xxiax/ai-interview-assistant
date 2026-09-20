from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app import asr, cost_control


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"text": "  转写结果  "}


class FakeClient:
    def __init__(self, capture, **_kwargs):
        self.capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.capture["url"] = url
        self.capture.update(kwargs)
        return FakeResponse()


def test_missing_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        asr._get_api_key()


def test_saved_config_takes_priority_over_env(monkeypatch):
    """设置页保存的 asr 配置优先于环境变量。"""
    from app import db as app_db

    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    conn = app_db.get_db()
    try:
        app_db.save_config(
            conn, "asr", "页面配置", {"model": "whisper-large-v3", "api_key": "saved-key"}, True
        )
        # save_config 已把 api_key 加密入库;无需额外插入
    finally:
        conn.close()
    assert asr._get_api_key() == "saved-key"


def test_media_probe_startup_check_is_fail_closed(monkeypatch):
    monkeypatch.delenv("AI_SKIP_MEDIA_PROBE_CHECK", raising=False)
    monkeypatch.setattr(asr.shutil, "which", lambda _path: None)
    monkeypatch.setenv("AI_FFPROBE_PATH", "definitely-missing-ffprobe")
    with pytest.raises(RuntimeError, match="ffprobe"):
        asr.validate_media_probe_available()

    monkeypatch.setenv("AI_SKIP_MEDIA_PROBE_CHECK", "true")
    asr.validate_media_probe_available()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("codec", "filename", "content_type"),
    [
        ("webm_opus", "chunk.webm", "audio/webm"),
        ("ogg_opus", "chunk.ogg", "audio/ogg"),
        ("m4a_aac", "chunk.m4a", "audio/mp4"),
        ("wav_pcm_s16le", "chunk.wav", "audio/wav"),
    ],
)
async def test_codec_metadata_is_forwarded(monkeypatch, codec, filename, content_type):
    capture = {}
    monkeypatch.setenv("AI_ASR_ENGINE", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "groq-secret")
    # 本用例验证 Groq 回退路径的元数据:强制 whisper 模型激活(FunASR 默认只吃 wav)
    from app import db as app_db

    conn = app_db.get_db()
    try:
        app_db.save_config(
            conn, "asr", "groq-回退", {"model": "whisper-large-v3", "api_key": "saved"}, True
        )
    finally:
        conn.close()
    monkeypatch.setattr(
        asr.httpx, "AsyncClient", lambda **kwargs: FakeClient(capture, **kwargs)
    )
    monkeypatch.setattr(asr, "inspect_audio", lambda *_args: 1000)
    result = await asr.transcribe_audio(b"audio", codec, declared_duration_ms=1000)
    assert result == "转写结果"
    assert capture["files"]["file"] == (filename, b"audio", content_type)
    assert capture["headers"]["Authorization"] == "Bearer saved"


@pytest.mark.asyncio
async def test_unsupported_codec_is_rejected_before_request(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "groq-secret")
    with pytest.raises(ValueError, match="编码"):
        await asr.transcribe_audio(b"audio", "mp3", declared_duration_ms=1000)


@pytest.mark.asyncio
async def test_malformed_asr_response_is_rejected(monkeypatch):
    class MalformedResponse(FakeResponse):
        def json(self):
            return ["not-an-object"]

    class MalformedClient(FakeClient):
        async def post(self, url, **kwargs):
            self.capture["url"] = url
            self.capture.update(kwargs)
            return MalformedResponse()

    monkeypatch.setenv("GROQ_API_KEY", "groq-secret")
    monkeypatch.setattr(
        asr.httpx, "AsyncClient", lambda **kwargs: MalformedClient({}, **kwargs)
    )
    monkeypatch.setattr(asr, "inspect_audio", lambda *_args: 1000)
    with pytest.raises(RuntimeError, match="返回格式"):
        await asr.transcribe_audio(b"audio", "webm_opus", declared_duration_ms=1000)


@pytest.mark.asyncio
async def test_asr_concurrency_limit_includes_media_probe(monkeypatch):
    active = 0
    peak = 0
    counter_lock = threading.Lock()

    def slow_probe(*_args):
        nonlocal active, peak
        with counter_lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.05)
            return 1000
        finally:
            with counter_lock:
                active -= 1

    async def fake_reserve(*_args):
        return None

    monkeypatch.setenv("GROQ_API_KEY", "groq-secret")
    monkeypatch.setenv("AI_ASR_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("AI_PAID_CALL_QUEUE_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr(asr, "inspect_audio", slow_probe)
    monkeypatch.setattr(asr.cost_control, "reserve_asr_millis", fake_reserve)
    monkeypatch.setattr(
        asr.httpx, "AsyncClient", lambda **kwargs: FakeClient({}, **kwargs)
    )

    await asyncio.gather(
        *(
            asr.transcribe_audio(
                f"audio-{index}".encode(),
                "webm_opus",
                declared_duration_ms=1000,
            )
            for index in range(8)
        )
    )
    assert peak <= 2


def test_audio_probe_rejects_forged_duration(monkeypatch):
    class ProbeResult:
        returncode = 0
        stdout = (
            '{"format":{"format_name":"webm","duration":"12.0"},'
            '"streams":[{"codec_type":"audio","codec_name":"opus"}]}'
        )

    monkeypatch.setattr(asr, "_ffprobe_path", lambda: "ffprobe")
    monkeypatch.setattr(asr.subprocess, "run", lambda *_args, **_kwargs: ProbeResult())
    with pytest.raises(ValueError, match="真实音频时长"):
        asr.inspect_audio(b"audio", "webm_opus", 100)


def test_audio_probe_rejects_wav_with_wrong_sample_format(monkeypatch):
    class ProbeResult:
        returncode = 0
        stdout = (
            '{"format":{"format_name":"wav","duration":"1.0"},'
            '"streams":[{"codec_type":"audio","codec_name":"pcm_s16le",'
            '"duration":"1.0","sample_rate":"44100","channels":2,'
            '"bits_per_sample":16}]}'
        )

    monkeypatch.setattr(asr, "_ffprobe_path", lambda: "ffprobe")
    monkeypatch.setattr(asr.subprocess, "run", lambda *_args, **_kwargs: ProbeResult())
    with pytest.raises(ValueError, match="16kHz"):
        asr.inspect_audio(_wav_bytes(), "wav_pcm_s16le", 1000)


# ---------- FunASR(自部署,默认引擎) ----------


def _fake_ws(monkeypatch, responses):
    """伪造 websockets.connect:捕获发送序列,按序回放服务端消息。"""
    import json as _json

    sent = []

    class FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, raw):
            if isinstance(raw, bytes):
                sent.append(("binary", len(raw)))
            else:
                sent.append(("text", _json.loads(raw)))

        async def recv(self):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return _json.dumps(item)

        async def close(self):
            pass

    def connect(url, **kwargs):
        sent.append(("connect", url))
        return FakeWS()

    monkeypatch.setattr(asr, "_websockets_connect", connect)
    return sent


def _wav_bytes(ms=1000):
    import io as _io
    import wave as _wave

    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * (16000 * ms // 1000))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_funasr_sends_start_pcm_stop_and_reads_final(monkeypatch):
    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    sent = _fake_ws(monkeypatch, [{"type": "started"}, {"type": "final", "text": "你好面试官"}])
    text = await asr.transcribe_audio(_wav_bytes(), "wav_pcm_s16le", declared_duration_ms=1000)
    assert text == "你好面试官"
    kinds = [s[0] for s in sent]
    # 顺序:connect → start(json) → binary(pcm) → stop(json)
    assert kinds == ["connect", "text", "binary", "text"]
    assert sent[1][1]["type"] == "start" and sent[1][1]["sample_rate"] == 16000
    # binary 载荷应为剥掉 WAV 头的裸 PCM(32000 字节 = 1s)
    assert sent[2][1] == 32000
    assert sent[3][1]["type"] == "stop"


@pytest.mark.asyncio
async def test_funasr_stream_keeps_one_utterance_open_across_chunks(monkeypatch):
    """一个语音段只发一次 start、一次 stop；段内每片只推 PCM 并拿累计 partial。"""
    import json as _json

    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    monkeypatch.setenv("AI_FUNASR_STREAM", "true")
    monkeypatch.setenv("AI_FUNASR_URL", "ws://127.0.0.1:10096/ws")
    monkeypatch.setenv("AI_FUNASR_PARTIAL_IDLE_SECONDS", "0.05")
    monkeypatch.setenv("AI_FUNASR_PARTIAL_MAX_WAIT_SECONDS", "0.5")
    monkeypatch.setattr(asr, "_active_proxy", lambda: None)
    monkeypatch.setattr(asr, "inspect_audio", lambda *_args: 1000)
    monkeypatch.setattr(asr, "_resolve_funasr_host", lambda *_args: _async_none())
    monkeypatch.setattr(asr.cost_control, "reserve_asr_millis", _async_noop)

    sent = []
    incoming = asyncio.Queue()

    class StreamWS:
        def __init__(self):
            self.pushes = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, raw):
            if isinstance(raw, bytes):
                self.pushes += 1
                sent.append(("binary", len(raw)))
                # partial 是累计全文：第二片包含第一片的文本
                text = "".join(f"第{index}段" for index in range(1, self.pushes + 1))
                await incoming.put({"type": "partial", "text": text})
            else:
                message = _json.loads(raw)
                sent.append(("text", message))
                if message["type"] == "start":
                    await incoming.put({"type": "started"})
                if message["type"] == "stop":
                    await incoming.put({"type": "final", "text": "第1段第2段"})

        async def recv(self):
            return _json.dumps(await incoming.get())

        async def close(self):
            return None

    connects = []

    def connect(url, **_kwargs):
        connects.append(url)
        return StreamWS()

    monkeypatch.setattr(asr, "_websockets_connect", connect)
    stream = asr.FunAsrStream()
    first = await stream.push_wav(_wav_bytes(), 1000)
    second = await stream.push_wav(_wav_bytes(), 1000)
    finished = await stream.finish()
    await stream.close()

    assert len(connects) == 1
    # 关键：整段只有一次 start、一次 stop，不再是每片 start/stop
    assert [item[1]["type"] for item in sent if item[0] == "text"] == ["start", "stop"]
    assert [item[0] for item in sent if item[0] == "binary"] == ["binary", "binary"]
    assert [event.type for event in first] == ["partial"]
    assert [event.type for event in second] == ["partial"]
    # partial 累计：第二片文本包含第一片
    assert first[-1].text == "第1段"
    assert second[-1].text == "第1段第2段"
    assert [event.type for event in finished] == ["final"]
    assert finished[-1].text == "第1段第2段"


@pytest.mark.asyncio
async def test_funasr_stream_starts_new_utterance_after_finish(monkeypatch):
    """finish 之后下一片重新发 start，同一连接承载多个语音段。"""
    import json as _json

    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    monkeypatch.setenv("AI_FUNASR_STREAM", "true")
    monkeypatch.setenv("AI_FUNASR_URL", "ws://127.0.0.1:10096/ws")
    monkeypatch.setenv("AI_FUNASR_PARTIAL_IDLE_SECONDS", "0.05")
    monkeypatch.setenv("AI_FUNASR_PARTIAL_MAX_WAIT_SECONDS", "0.5")
    monkeypatch.setattr(asr, "_active_proxy", lambda: None)
    monkeypatch.setattr(asr, "inspect_audio", lambda *_args: 1000)
    monkeypatch.setattr(asr, "_resolve_funasr_host", lambda *_args: _async_none())
    monkeypatch.setattr(asr.cost_control, "reserve_asr_millis", _async_noop)

    sent = []
    incoming = asyncio.Queue()

    class StreamWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, raw):
            if isinstance(raw, bytes):
                sent.append("binary")
                await incoming.put({"type": "partial", "text": "问题"})
            else:
                message = _json.loads(raw)
                sent.append(message["type"])
                if message["type"] == "stop":
                    await incoming.put({"type": "final", "text": "问题？"})

        async def recv(self):
            return _json.dumps(await incoming.get())

        async def close(self):
            return None

    connects = []

    def connect(url, **_kwargs):
        connects.append(url)
        return StreamWS()

    monkeypatch.setattr(asr, "_websockets_connect", connect)
    stream = asr.FunAsrStream()
    await stream.push_wav(_wav_bytes(), 1000)
    await stream.finish()
    await stream.push_wav(_wav_bytes(), 1000)
    await stream.finish()
    # finish 后没有活动 utterance，重复 finish 是空操作
    assert await stream.finish() == []
    await stream.close()

    assert len(connects) == 1
    assert sent == ["start", "binary", "stop", "start", "binary", "stop"]


class _IdleWS:
    """已连接但不返回任何服务端消息的空闲 FunASR 长连接。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def send(self, _raw):
        return None

    async def recv(self):
        await asyncio.Event().wait()

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_idle_funasr_streams_do_not_hold_asr_slots(monkeypatch):
    """A4：已连接、未推流的 FunASR 长连接不得占住付费并发门。

    连接跨语音段复用；若连接期持有 slot，一个 radio_mode=both 会话的
    两条空闲流就吃满默认 AI_ASR_MAX_CONCURRENCY=2，第二个会话的任何
    转写调用都会 PaidCallBusyError。
    """
    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    monkeypatch.setenv("AI_FUNASR_URL", "ws://127.0.0.1:10096/ws")
    monkeypatch.setenv("AI_ASR_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("AI_PAID_CALL_QUEUE_TIMEOUT_SECONDS", "0.2")
    cost_control.reset_runtime_state()
    monkeypatch.setattr(asr, "_active_proxy", lambda: None)
    monkeypatch.setattr(asr, "_resolve_funasr_host", lambda *_args: _async_none())
    monkeypatch.setattr(asr, "_websockets_connect", lambda _url, **_kw: _IdleWS())

    stream_a = asr.FunAsrStream()
    stream_b = asr.FunAsrStream()
    await stream_a.connect()
    await stream_b.connect()
    try:
        # 两条空闲流不占许可：默认 2 个许可必须全部可用（模拟第二会话的转写调用）
        async with cost_control.paid_call_slot("asr"):
            async with cost_control.paid_call_slot("asr"):
                pass
    finally:
        await stream_a.close()
        await stream_b.close()


@pytest.mark.asyncio
async def test_active_push_holds_exactly_one_asr_slot(monkeypatch):
    """推流窗口保持一调用一许可：活跃 push 占 1 个，推完立即归还。"""
    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    monkeypatch.setenv("AI_FUNASR_URL", "ws://127.0.0.1:10096/ws")
    monkeypatch.setenv("AI_ASR_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("AI_PAID_CALL_QUEUE_TIMEOUT_SECONDS", "0.2")
    cost_control.reset_runtime_state()
    monkeypatch.setattr(asr, "_active_proxy", lambda: None)
    monkeypatch.setattr(asr, "inspect_audio", lambda *_args: 1000)
    monkeypatch.setattr(asr, "_resolve_funasr_host", lambda *_args: _async_none())
    monkeypatch.setattr(asr.cost_control, "reserve_asr_millis", _async_noop)
    monkeypatch.setattr(asr, "_websockets_connect", lambda _url, **_kw: _IdleWS())

    stream = asr.FunAsrStream()
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()

    async def controlled_drain(*_args, **_kwargs):
        drain_started.set()
        await release_drain.wait()
        return []

    monkeypatch.setattr(stream, "_drain_until_idle", controlled_drain)
    push = asyncio.create_task(stream.push_wav(_wav_bytes(), 1000))
    await drain_started.wait()

    # 活跃推流恰好占 1 个许可：剩下 1 个可获取，第 3 次必须 Busy
    async with cost_control.paid_call_slot("asr"):
        with pytest.raises(cost_control.PaidCallBusyError):
            async with cost_control.paid_call_slot("asr"):
                pass
    release_drain.set()
    await push
    await stream.close()

    # 推流结束即归还：2 个许可重新全部可用
    async with cost_control.paid_call_slot("asr"):
        async with cost_control.paid_call_slot("asr"):
            pass


def test_funasr_stream_is_enabled_by_default_for_wav(monkeypatch):
    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    monkeypatch.delenv("AI_FUNASR_STREAM", raising=False)

    assert asr.use_funasr_stream("wav_pcm_s16le") is True
    assert asr.use_funasr_stream("webm_opus") is False

    monkeypatch.setenv("AI_FUNASR_STREAM", "false")
    assert asr.use_funasr_stream("wav_pcm_s16le") is False


def test_partial_idle_window_stays_below_the_default_chunk(monkeypatch):
    """IDLE 是每片必付的等待，必须小于客户端默认分片时长(400 ms)。

    大于分片时长的话，音频 worker 的消费速度就低于采集线程的生产速度，
    队列填满后客户端背压回踩，首字延迟反而比大分片更差。
    """
    monkeypatch.delenv("AI_FUNASR_PARTIAL_IDLE_SECONDS", raising=False)
    monkeypatch.delenv("AI_FUNASR_PARTIAL_MAX_WAIT_SECONDS", raising=False)

    assert asr._funasr_partial_idle_seconds() < 0.4
    assert asr._funasr_partial_max_wait_seconds() >= asr._funasr_partial_idle_seconds()


@pytest.mark.asyncio
async def test_drain_returns_queued_partial_without_waiting():
    """队列里已有 partial 时必须立刻返回，不能再白等一个空闲窗口。"""
    stream = asr.FunAsrStream()
    await stream._put_event(asr.FunAsrEvent("partial", text="第1段"))
    await stream._put_event(asr.FunAsrEvent("partial", text="第1段第2段"))

    started = time.monotonic()
    events = await stream._drain_until_idle(5.0, 15.0)
    elapsed = time.monotonic() - started

    assert [event.text for event in events] == ["第1段", "第1段第2段"]
    assert elapsed < 0.1, f"队列已有事件却等了 {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_drain_waits_at_most_one_idle_window_when_queue_is_empty():
    """空队列时最多付一个 idle 窗口，不是每条事件后都再等一次。"""
    stream = asr.FunAsrStream()

    started = time.monotonic()
    assert await stream._drain_until_idle(0.05, 5.0) == []
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"空队列等了 {elapsed:.3f}s，超过一个 idle 窗口"


@pytest.mark.asyncio
async def test_drain_does_not_wait_again_after_taking_one_event():
    """拿到一条就返回：不能为"可能更新的下一条"再守一个窗口。

    partial 是累计全文，最新一条就够；下一条会在下个分片推流时被扫走。
    """
    stream = asr.FunAsrStream()

    async def put_later():
        await asyncio.sleep(0.01)
        await stream._put_event(asr.FunAsrEvent("partial", text="第1段"))

    task = asyncio.create_task(put_later())
    started = time.monotonic()
    events = await stream._drain_until_idle(1.0, 5.0)
    elapsed = time.monotonic() - started
    await task

    assert [event.text for event in events] == ["第1段"]
    assert elapsed < 0.5, f"取到事件后又等了一轮，共 {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_drain_raises_on_error_event():
    stream = asr.FunAsrStream()
    await stream._put_event(asr.FunAsrEvent("error", message="鉴权失败"))
    with pytest.raises(RuntimeError, match="鉴权失败"):
        await stream._drain_until_idle(0.05, 0.5)


@pytest.mark.asyncio
async def test_short_chunks_reserve_exact_milliseconds(monkeypatch):
    """400 ms 分片必须记 400，不能向上取整成一整秒。

    向上取整的话，一分钟连续说话会记成 150 秒，
    AI_ASR_SECONDS_PER_MINUTE 在真正说满一分钟前就先爆了。
    """
    reserved: list[int] = []

    async def fake_reserve(_credential, millis):
        reserved.append(millis)

    monkeypatch.setenv("AI_FUNASR_TOKEN", "funasr-token")
    monkeypatch.setattr(asr, "inspect_audio", lambda *_args: 400)
    monkeypatch.setattr(asr.cost_control, "reserve_asr_millis", fake_reserve)

    stream = asr.FunAsrStream()

    class NoopWS:
        async def send(self, _raw):
            return None

    stream._ws = NoopWS()
    monkeypatch.setattr(stream, "connect", _async_noop)
    monkeypatch.setattr(stream, "_drain_until_idle", _async_empty_list)

    await stream.push_wav(_wav_bytes(), 400)
    assert reserved == [400]


async def _async_none(*_args):
    return None


async def _async_noop(*_args, **_kwargs):
    return None


async def _async_empty_list(*_args, **_kwargs):
    return []


@pytest.mark.asyncio
async def test_funasr_error_message_is_surfaced(monkeypatch):
    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    _fake_ws(monkeypatch, [{"type": "error", "message": "鉴权失败"}])
    with pytest.raises(RuntimeError, match="鉴权失败"):
        await asr.transcribe_audio(_wav_bytes(), "wav_pcm_s16le", declared_duration_ms=1000)


@pytest.mark.asyncio
async def test_funasr_timeout_raises(monkeypatch):
    """服务端迟迟不回 final：必须在 deadline 内超时，而不是永久等待。"""
    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")

    class SlowWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, raw):
            return None

        async def recv(self):
            await asyncio.sleep(30)
            return "{}"

        async def close(self):
            return None

    monkeypatch.setattr(asr, "_websockets_connect", lambda url, **kw: SlowWS())
    monkeypatch.setattr(asr, "inspect_audio", lambda *a: 1000)
    # recv 被 wait_for(deadline) 包裹：静默服务端最终以超时错误终止，
    # 不会永久挂起。上限 1s 音频 → deadline = max(10, 1+8) = 10s
    with pytest.raises((asyncio.TimeoutError, RuntimeError)):
        await asyncio.wait_for(
            asr.transcribe_audio(
                _wav_bytes(), "wav_pcm_s16le", declared_duration_ms=1000
            ),
            timeout=14,
        )


@pytest.mark.asyncio
async def test_funasr_http_401_raises_structured_auth_error(monkeypatch):
    """FunASR 握手 401 必须抛 AsrAuthError（带状态码），不是泛型连接失败。

    这是 B4 的核心：此前 401 靠 "groq.com" in str(exc) 字符串判定，
    FunASR 的 401 不含 groq.com 会被误分类为可重试 processing_failed。
    """
    monkeypatch.setenv("AI_ASR_ENGINE", "funasr")
    from websockets.exceptions import InvalidStatus

    class FakeResponse:
        status_code = 401

    class FailingWS:
        async def __aenter__(self):
            raise InvalidStatus(FakeResponse())

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        asr, "_websockets_connect", lambda url, **kw: FailingWS()
    )
    monkeypatch.setattr(asr, "inspect_audio", lambda *a: 1000)
    with pytest.raises(asr.AsrAuthError) as exc_info:
        await asr.transcribe_audio(
            _wav_bytes(), "wav_pcm_s16le", declared_duration_ms=1000
        )
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_groq_http_401_raises_structured_auth_error(monkeypatch):
    """Groq 401/403 抛 AsrAuthError（结构化状态码），供 realtime 分类。"""
    from app import db as app_db

    conn = app_db.get_db()
    try:
        app_db.save_config(
            conn,
            "asr",
            "groq-401",
            {"model": "whisper-large-v3", "api_key": "saved"},
            True,
        )
    finally:
        conn.close()

    class Resp:
        status_code = 401

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            return Resp()

    monkeypatch.setattr(asr.httpx, "AsyncClient", lambda **kw: Client(**kw))
    monkeypatch.setattr(asr, "inspect_audio", lambda *a: 1000)
    with pytest.raises(asr.AsrAuthError) as exc_info:
        await asr.transcribe_audio(b"x", "webm_opus", declared_duration_ms=1000)
    assert exc_info.value.status_code == 401


def test_missing_api_key_raises_config_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(asr.AsrConfigError):
        asr._get_api_key()


def test_wav_payload_strips_header_and_handles_padding():
    """_wav_payload 纯函数：正常剥离、奇数 padding、缺 data chunk、恶意 chunk_size。"""
    # 正常 44 字节头 + 4 字节数据
    pcm = b"\x01\x02\x03\x04"
    wav = _wav_bytes_with_payload(pcm)
    assert asr._wav_payload(wav) == pcm

    # data chunk 前有一个奇数长度的 LIST chunk（必须按 chunk_size+pad 前进）
    import struct

    odd_payload = b"abc"  # 奇数长度,pad 1 字节
    chunks = b"RIFF" + struct.pack("<I", 4 + 8 + len(odd_payload) + 1 + 8 + 4) + b"WAVE"
    chunks += b"LIST" + struct.pack("<I", len(odd_payload)) + odd_payload + b"\x00"
    chunks += b"data" + struct.pack("<I", 4) + pcm
    assert asr._wav_payload(chunks) == pcm

    # 缺 data chunk
    no_data = b"RIFF" + struct.pack("<I", 4) + b"WAVE"
    with pytest.raises(ValueError, match="data chunk"):
        asr._wav_payload(no_data)

    # 恶意 chunk_size（巨大）：offset 前进越界后循环终止,报缺 data chunk
    evil = (
        b"RIFF" + struct.pack("<I", 4 + 8 + 8)
        + b"WAVE"
        + b"LIST" + struct.pack("<I", 0xFFFFFFF0) + b"xx"
    )
    with pytest.raises(ValueError, match="data chunk"):
        asr._wav_payload(evil)

    # 不是 RIFF
    with pytest.raises(ValueError, match="WAV"):
        asr._wav_payload(b"XXXX" + b"\x00" * 20)


def _wav_bytes_with_payload(payload: bytes) -> bytes:
    import struct

    data_len = len(payload)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_len)
        + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
        + b"data" + struct.pack("<I", data_len)
        + payload
    )


@pytest.mark.asyncio
async def test_whisper_model_falls_back_to_groq(monkeypatch):
    from app import db as app_db

    conn = app_db.get_db()
    try:
        app_db.save_config(
            conn,
            "asr",
            "回退",
            {"model": "whisper-large-v3", "api_key": "gsk-fallback"},
            True,
        )
    finally:
        conn.close()

    capture = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"text": "groq 结果"}

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            capture["headers"] = kwargs["headers"]
            return FakeResp()

    monkeypatch.setattr(asr.httpx, "AsyncClient", lambda **kw: FakeClient(**kw))
    monkeypatch.setattr(asr, "inspect_audio", lambda *a: 1000)
    text = await asr.transcribe_audio(b"x", "webm_opus", declared_duration_ms=1000)
    assert text == "groq 结果"
    assert capture["headers"]["Authorization"] == "Bearer gsk-fallback"


def test_legacy_groq_named_config_does_not_send_paraformer_model():
    from app import db as app_db

    conn = app_db.get_db()
    try:
        app_db.save_config(
            conn,
            "asr",
            "groq",
            {"model": "paraformer-zh-streaming", "api_key": "gsk-legacy"},
            True,
        )
    finally:
        conn.close()

    assert asr._get_model() == asr.GROQ_MODEL


def test_windows_proxy_server_parser_prefers_https_and_adds_scheme():
    assert (
        asr._normalize_proxy_url("http=127.0.0.1:7890;https=127.0.0.1:7897")
        == "http://127.0.0.1:7897"
    )
    assert asr._normalize_proxy_url("127.0.0.1:7897") == "http://127.0.0.1:7897"
    assert asr._normalize_proxy_url("not a proxy") is None


def test_explicit_outbound_proxy_wins(monkeypatch):
    monkeypatch.setenv("AI_OUTBOUND_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setattr(asr, "_windows_system_proxy", lambda: "http://127.0.0.1:7897")
    assert asr._active_proxy() == "socks5://127.0.0.1:1080"


@pytest.mark.asyncio
async def test_funasr_fake_dns_address_uses_doh_fallback(monkeypatch):
    monkeypatch.setattr(
        asr.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("198.18.42.62", 443))],
    )

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"Answer": [{"type": 1, "data": "113.96.24.42"}]}

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(asr.httpx, "AsyncClient", Client)
    assert await asr._resolve_funasr_host("asr.xiaoxia.pro", None) == "113.96.24.42"


def test_local_funasr_host_is_not_sent_through_proxy():
    assert asr._is_local_host("127.0.0.1") is True
    assert asr._is_local_host("localhost") is True
    assert asr._is_local_host("::1") is True
    assert asr._is_local_host("asr.xiaoxia.pro") is False
