"""音频转写路由：默认 FunASR，LLM/Groq 仅作显式备用。"""

import asyncio
import ipaddress
import json
import math
import os
import random
import shutil
import socket
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx2 as httpx

from . import cost_control, db

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"


class AsrConfigError(RuntimeError):
    """ASR 配置缺失（未保存 Key 且无环境变量）。不可重试。"""


class AsrAuthError(RuntimeError):
    """转写服务以 401/403 拒绝请求（Key 无效或无权限）。不可重试。

    error_code 保持协议兼容：realtime 侧据此归类为 config_missing
    一类的不可重试配置错误，客户端不会盲目重试。
    """

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _normalize_proxy_url(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    if ";" in candidate or "=" in candidate:
        mapped: dict[str, str] = {}
        fallback = ""
        for item in candidate.split(";"):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                scheme, address = item.split("=", 1)
                mapped[scheme.strip().lower()] = address.strip()
            elif not fallback:
                fallback = item
        candidate = mapped.get("https") or mapped.get("http") or fallback
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parsed = urlparse(candidate)
    if (
        parsed.scheme not in {"http", "https", "socks5", "socks5h"}
        or not parsed.hostname
        or any(char.isspace() for char in parsed.netloc)
    ):
        return None
    return candidate


def _windows_system_proxy() -> str | None:
    """读取当前 Windows 用户的 Internet Settings 代理。

    Clash Verge 等桌面代理通常只写这里，不会设置 HTTP_PROXY；忽略它会让
    fake-IP（198.18/15）被后端当成真实地址直连并在 TLS 握手阶段超时。
    """
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            if not enabled:
                return None
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0])
    except (OSError, TypeError, ValueError):
        return None
    return _normalize_proxy_url(server)


def _is_fake_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value) in FAKE_IP_NETWORK
    except ValueError:
        return False


def _is_local_host(hostname: str | None) -> bool:
    if not hostname or hostname.lower() == "localhost":
        return hostname is not None
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


async def _resolve_funasr_host(hostname: str, proxy_url: str | None) -> str | None:
    """当本机 DNS 返回 fake-IP 时，通过 DoH 获取真实地址。"""
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, hostname, 443, type=socket.SOCK_STREAM
        )
    except OSError:
        addresses = []
    resolved = {
        str(item[4][0])
        for item in addresses
        if item[4] and isinstance(item[4][0], str)
    }
    if not resolved or not all(_is_fake_ip(address) for address in resolved):
        return None

    doh_url = os.environ.get("AI_DNS_OVER_HTTPS_URL", "https://dns.google/resolve").strip()
    kwargs = {
        "timeout": httpx.Timeout(10.0, connect=5.0),
        "follow_redirects": False,
        "trust_env": False,
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(
                doh_url,
                params={"name": hostname, "type": "A"},
                headers={"accept": "application/dns-json"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, OSError, ValueError, TypeError):
        return None
    answers = payload.get("Answer", []) if isinstance(payload, dict) else []
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        address = answer.get("data")
        if not isinstance(address, str) or _is_fake_ip(address):
            continue
        try:
            ipaddress.ip_address(address)
        except ValueError:
            continue
        return address
    return None


def _active_proxy() -> str | None:
    """代理优先级：部署环境覆盖 → 设置页配置(空=显式直连) → Windows 当前用户代理。"""
    override = _normalize_proxy_url(os.environ.get("AI_OUTBOUND_PROXY", ""))
    if override:
        return override
    try:
        conn = db.get_db()
        try:
            config = db.get_active_config(conn, "network")
        finally:
            conn.close()
        if config:
            url = config["data"].get("proxy_url")
            if isinstance(url, str):
                if not url.strip():
                    # 激活的网络配置显式选择"直连"：出网一律不走代理，
                    # 也不再回退 Windows 系统代理。
                    return None
                normalized = _normalize_proxy_url(url)
                if normalized:
                    return normalized
    except Exception:  # noqa: BLE001,S110 - 代理配置损坏时按优先级继续降级
        pass
    return _windows_system_proxy()


def http_client_kwargs(timeout: httpx.Timeout) -> dict:
    """统一的出网 HTTP 客户端参数:默认直连;设置页配置了代理则显式走代理。

    代理支持 http/https/socks5/socks5h(socks 需要 httpx[socks] 依赖)。
    环境变量 AI_OUTBOUND_PROXY 可临时覆盖(部署调试用)。
    """
    proxy = _active_proxy()
    kwargs: dict = {"timeout": timeout, "follow_redirects": False, "trust_env": False}
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs
CODEC_DETAILS = {
    "webm_opus": ("chunk.webm", "audio/webm"),
    "ogg_opus": ("chunk.ogg", "audio/ogg"),
    "m4a_aac": ("chunk.m4a", "audio/mp4"),
    "wav_pcm_s16le": ("chunk.wav", "audio/wav"),
}
CODEC_PROBE_EXPECTATIONS = {
    "webm_opus": ({"matroska", "webm"}, {"opus"}),
    "ogg_opus": ({"ogg"}, {"opus"}),
    "m4a_aac": ({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}, {"aac"}),
    "wav_pcm_s16le": ({"wav"}, {"pcm_s16le"}),
}

# 本地开发通过 ssh -N -L 暴露同机端口；生产也按同机部署设计。
FUNASR_DEFAULT_URL = "ws://127.0.0.1:10096/ws"
ASR_ENGINE_VALUES = {"llm", "funasr", "groq"}
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _get_api_key() -> str:
    """获取 Groq API key：优先取设置页保存的 asr 配置，回退环境变量。"""
    try:
        conn = db.get_db()
        try:
            config = db.get_active_config(conn, "asr")
        finally:
            conn.close()
    except Exception as exc:  # 数据库不可用时不阻断环境变量路径
        raise RuntimeError(f"读取 ASR 配置失败：{exc}") from exc
    if config and config["data"].get("api_key"):
        return config["data"]["api_key"]
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise AsrConfigError(
            "未配置 ASR：请在客户端设置页保存 Groq API Key，或设置 GROQ_API_KEY 环境变量"
        )
    return key


def _get_model() -> str:
    """获取转写模型：asr 配置可覆盖，默认 whisper-large-v3。"""
    try:
        conn = db.get_db()
        try:
            config = db.get_active_config(conn, "asr")
        finally:
            conn.close()
        if config and config["data"].get("model"):
            model = str(config["data"]["model"])
            # 旧版设置页允许把配置命名为 Groq、却保存了 FunASR 模型名。
            # Groq API 不认识 paraformer；按提供商名称修正为有效默认模型。
            if "groq" in str(config.get("name", "")).lower() and not model.startswith(
                "whisper"
            ):
                return GROQ_MODEL
            return model
    except Exception:  # noqa: BLE001,S110 - 配置不可读时回退默认 Groq 模型
        pass
    return GROQ_MODEL


def _ffprobe_path() -> str:
    configured = os.environ.get("AI_FFPROBE_PATH", "ffprobe").strip()
    resolved = shutil.which(configured)
    if resolved:
        return resolved
    path = Path(configured)
    if path.is_file():
        return str(path)
    raise RuntimeError("未找到 ffprobe；生产镜像必须安装 ffmpeg/ffprobe")


def validate_media_probe_available() -> None:
    if os.environ.get("AI_SKIP_MEDIA_PROBE_CHECK", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    _ffprobe_path()


def validate_funasr_config() -> None:
    """校验 ASR 引擎；默认使用 FunASR，其他引擎按需启用。"""
    engine = os.environ.get("AI_ASR_ENGINE", "funasr").strip().lower()
    if engine not in ASR_ENGINE_VALUES:
        raise RuntimeError("AI_ASR_ENGINE 只能是 llm、funasr 或 groq")
    if engine != "funasr":
        return
    _funasr_token()
    url = os.environ.get("AI_FUNASR_URL", FUNASR_DEFAULT_URL).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise RuntimeError("AI_FUNASR_URL 必须是包含主机的 ws:// 或 wss:// 地址")


async def check_funasr_ready(timeout_seconds: float = 2.0) -> None:
    """验证 FunASR TCP/TLS 端点可达，不发送 Token 或业务音频。"""
    url = _funasr_url()
    parsed = urlparse(url)
    if not parsed.hostname:
        raise RuntimeError("FunASR 地址缺少主机")
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    ssl_context = ssl.create_default_context() if parsed.scheme == "wss" else None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                parsed.hostname,
                port,
                ssl=ssl_context,
                server_hostname=parsed.hostname if ssl_context else None,
            ),
            timeout=max(0.1, timeout_seconds),
        )
    except (OSError, asyncio.TimeoutError, ssl.SSLError) as exc:
        raise RuntimeError("FunASR 端点不可达") from exc
    writer.close()
    await writer.wait_closed()


async def check_paid_asr_config() -> None:
    """非 FunASR 引擎的就绪检查：验证实际付费转写路径的配置已存在。

    engine=llm 需要激活的 LLM 配置填写了 model（与 transcribe_audio 的
    路由要求一致）；engine=groq 需要已配置 Groq Key（设置页或
    GROQ_API_KEY）。未配置时抛 RuntimeError，由 /health/ready 转为 503。
    配置读取同步打开 SQLite，移出事件循环（对齐 llm._get_llm_config_async）。
    """
    engine = os.environ.get("AI_ASR_ENGINE", "funasr").strip().lower()
    if engine == "groq":
        try:
            await asyncio.to_thread(_get_api_key)
        except RuntimeError as exc:
            raise RuntimeError(
                "Groq 转写未配置：请设置 GROQ_API_KEY 或在设置页保存 API Key"
            ) from exc
    elif engine == "llm":
        if not await asyncio.to_thread(_llm_model):
            raise RuntimeError("LLM 转写未配置：激活的 LLM 配置缺少 model")
    elif engine not in ASR_ENGINE_VALUES:
        # 未知引擎(如 AI_ASR_ENGINE 拼写错误)不得静默放行,否则未配置的
        # 部署被报告为健康。
        raise RuntimeError("AI_ASR_ENGINE 只能是 llm、funasr 或 groq")


def inspect_audio(audio_bytes: bytes, codec: str, declared_duration_ms: int) -> int:
    """解析媒体容器，返回服务端测得的真实时长（毫秒）。"""
    if codec not in CODEC_DETAILS:
        raise ValueError("不支持的音频编码")
    suffix = Path(CODEC_DETAILS[codec][0]).suffix
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
            temp_file.write(audio_bytes)
            temp_path = temp_file.name
        result = subprocess.run(
            [
                _ffprobe_path(),
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration:stream=codec_type,codec_name,duration,sample_rate,channels,bits_per_sample",
                "-of",
                "json",
                temp_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("音频容器无法解析")
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise ValueError("音频探测结果无效") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("音频安全探测失败") from exc
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)

    format_data = payload.get("format", {})
    streams = payload.get("streams", [])
    if not isinstance(format_data, dict) or not isinstance(streams, list):
        raise TypeError("音频探测结果无效")
    audio_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if len(audio_streams) != 1:
        raise ValueError("音频分片必须且只能包含一个音轨")

    expected_formats, expected_codecs = CODEC_PROBE_EXPECTATIONS[codec]
    formats = {
        item.strip().lower()
        for item in str(format_data.get("format_name", "")).split(",")
        if item.strip()
    }
    actual_codec = str(audio_streams[0].get("codec_name", "")).lower()
    if (
        not formats.intersection(expected_formats)
        or actual_codec not in expected_codecs
    ):
        raise ValueError("音频容器或编码与声明不一致")

    if codec == "wav_pcm_s16le":
        stream = audio_streams[0]
        try:
            sample_rate = int(stream.get("sample_rate"))
            channels = int(stream.get("channels"))
            bits_per_sample = int(stream.get("bits_per_sample"))
        except (TypeError, ValueError) as exc:
            raise ValueError("WAV 音频缺少有效采样率、声道数或位深") from exc
        if (sample_rate, channels, bits_per_sample) != (16_000, 1, 16):
            raise ValueError("WAV 音频必须是 16kHz、单声道、16-bit PCM")

    raw_duration = audio_streams[0].get("duration") or format_data.get("duration")
    try:
        duration_ms = math.ceil(float(raw_duration) * 1000)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("无法确定真实音频时长") from exc
    max_duration_ms = int(os.environ.get("AI_MAX_AUDIO_DURATION_MS", "10000"))
    tolerance_ms = int(os.environ.get("AI_AUDIO_DURATION_TOLERANCE_MS", "750"))
    if duration_ms < 100 or duration_ms > max_duration_ms + 250:
        raise ValueError("真实音频时长超出允许范围")
    allowed_difference = max(tolerance_ms, int(duration_ms * 0.2))
    if abs(duration_ms - declared_duration_ms) > allowed_difference:
        raise ValueError("客户端声明时长与真实音频时长不一致")
    return duration_ms


async def transcribe_audio(
    audio_bytes: bytes,
    codec: str,
    source: str = "pc",
    declared_duration_ms: int = 0,
) -> str:
    """验证真实媒体时长后按显式引擎路由转写。"""
    if codec not in CODEC_DETAILS:
        raise ValueError("不支持的音频编码")
    if declared_duration_ms <= 0:
        raise ValueError("缺少客户端声明的音频时长")

    engine = os.environ.get("AI_ASR_ENGINE", "funasr").strip().lower()
    if engine not in ASR_ENGINE_VALUES:
        raise AsrConfigError("AI_ASR_ENGINE 只能是 llm、funasr 或 groq")
    if engine == "llm" and codec == "wav_pcm_s16le":
        llm_model = await asyncio.to_thread(_llm_model)
        if not llm_model:
            raise AsrConfigError(
                "未配置多模态转写：请在激活的 LLM 配置中填写 model"
            )
        from . import llm  # 延迟导入:llm.py 顶层已 import asr,避免模块环

        return await llm.transcribe_via_llm(audio_bytes, llm_model, declared_duration_ms)

    if engine == "funasr" and codec == "wav_pcm_s16le":
        return await _transcribe_funasr(audio_bytes, declared_duration_ms)
    return await _transcribe_groq(audio_bytes, codec, declared_duration_ms)


def _llm_model() -> str | None:
    """读取激活 LLM 配置的主模型。

    对齐 _use_groq_fallback 的安全 try/except 模式:转写路由不能因为
    LLM 配置缺失或数据库不可用而中断 FunASR/Groq 的默认路径。
    """
    try:
        conn = db.get_db()
        try:
            config = db.get_active_config(conn, "llm")
        finally:
            conn.close()
        if config:
            model = str(config["data"].get("model", "") or "").strip()
            return model or None
    except Exception:  # noqa: BLE001,S110 - 缺少 LLM 配置由调用边界统一报错
        pass
    return None


# ================= FunASR(默认 ASR 引擎) =================
# AI_ASR_ENGINE=funasr 时启用。文档: https://asr.xiaoxia.pro/docs


def _funasr_url() -> str:
    return os.environ.get("AI_FUNASR_URL", FUNASR_DEFAULT_URL).strip().rstrip("/")


def _funasr_token() -> str:
    token = os.environ.get("AI_FUNASR_TOKEN", "").strip()
    if not token:
        raise AsrConfigError("AI_ASR_ENGINE=funasr 时必须设置 AI_FUNASR_TOKEN")
    return token


def use_funasr_stream(codec: str) -> bool:
    """实时 WAV 走 FunASR 长连接:一个语音段一个 utterance,跨分片保留声学上下文。"""
    return (
        os.environ.get("AI_ASR_ENGINE", "funasr").strip().lower() == "funasr"
        and codec == "wav_pcm_s16le"
        and os.environ.get("AI_FUNASR_STREAM", "true").strip().lower() == "true"
    )


def _funasr_env_float(name: str, default: float, *, low: float, high: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if math.isnan(value) or value < low or value > high:
        return default
    return value


def _funasr_partial_idle_seconds() -> float:
    """推流后等待新 partial 的空闲窗口(秒)。

    这是每个分片必然要付出的等待:循环末尾一定靠超时退出。分片时长(客户端
    `AI_AUDIO_CHUNK_MS`,默认 400 ms)比这个值小的时候,音频 worker 追不上采集
    线程,队列填满后背压回踩,首字反而更慢。默认值因此压到 0.2 秒,让 worker
    在 400 ms 分片下仍有余量。
    """
    return _funasr_env_float(
        "AI_FUNASR_PARTIAL_IDLE_SECONDS", 0.2, low=0.05, high=5.0
    )


def _funasr_partial_max_wait_seconds() -> float:
    """单个分片推流后收取 partial 的总上限(秒),防止拖慢音频 worker。"""
    return _funasr_env_float(
        "AI_FUNASR_PARTIAL_MAX_WAIT_SECONDS", 1.0, low=0.2, high=15.0
    )


def _websocket_status_code(exc: BaseException) -> int | None:
    """从 websockets 异常中提取握手状态码（InvalidStatus 带 response.status_code）。"""
    import websockets.exceptions

    if isinstance(exc, websockets.exceptions.InvalidStatus):
        return exc.response.status_code
    # 旧版本抛 InvalidStatusCode，其属性为 status_code
    if isinstance(exc, websockets.exceptions.InvalidStatusCode):
        return exc.status_code
    return None


def _websockets_connect(url: str, *, proxy_url: str | None = None, **kwargs):
    """可注入的连接工厂(单测替换点)。

    真实实现:优先走设置页 network 配置的代理(python-socks 隧道);
    未配置代理时直连(适用于 Clash TUN 全局接管或服务可直连的网络)。
    proxy_url 由调用方在 asyncio.to_thread 中先解析好(_active_proxy 会同步
    打开 SQLite),本函数自身不再访问数据库。
    """
    import websockets

    if proxy_url:
        from python_socks.async_.asyncio import Proxy

        async def _connect():
            parsed = urlparse(url)
            proxy = Proxy.from_url(proxy_url)
            sock = await proxy.connect(
                dest_host=parsed.hostname,
                dest_port=parsed.port or (443 if parsed.scheme == "wss" else 80),
                timeout=10,
            )
            return await websockets.connect(url, sock=sock, **kwargs).__aenter__()

        return _WSWrapper(_connect())
    return websockets.connect(url, **kwargs)


class _WSWrapper:
    """把 awaitable 包装成具备 __aenter__/__aexit__ 的对象,统一调用方写法。"""

    def __init__(self, coro):
        self._coro = coro

    async def __aenter__(self):
        return await self._coro

    async def __aexit__(self, *args):
        return False


def _wav_payload(audio_bytes: bytes) -> bytes:
    """解析 WAV 头,返回 data chunk 的裸 PCM 载荷。"""
    import struct

    if len(audio_bytes) < 12 or audio_bytes[:4] != b"RIFF":
        raise ValueError("非 WAV 音频")
    offset = 12
    while offset + 8 <= len(audio_bytes):
        chunk_id = audio_bytes[offset : offset + 4]
        (chunk_size,) = struct.unpack_from("<I", audio_bytes, offset + 4)
        if chunk_id == b"data":
            start = offset + 8
            return audio_bytes[start : start + chunk_size]
        offset += 8 + chunk_size + (chunk_size & 1)
    raise ValueError("WAV 中缺少 data chunk")


@dataclass(frozen=True)
class FunAsrEvent:
    type: str
    text: str = ""
    message: str = ""


class FunAsrStream:
    """一个 session/source 复用的 FunASR WebSocket 长连接与开放式 utterance。

    一个语音段(speech segment)对应一个 utterance:`start` 只在语音段开始时
    发一次,随后每个音频分片只推裸 PCM,**不发 stop**,`stop` 只在客户端
    `speech_end` 到达时发一次。这样 FunASR 才能跨分片保留声学上下文,
    避免词被切在分片边界上(例如 DNS 只剩 NS、TCP 被拆到两片)。

    按网关文档(`{ASR}/docs`),`partial` 是**当前 utterance 的累计全文**,
    客户端应整段替换显示;`final` 是段末固化文本。因此上层不需要自己拼接
    分片文本:最新 partial 就是"分片 1..n"的全文。

    读取任务和发送任务分离,避免接收循环阻塞发送路径。
    """

    def __init__(self) -> None:
        self._ws = None
        self._ws_cm = None
        self._reader: asyncio.Task | None = None
        self._events: asyncio.Queue[FunAsrEvent] = asyncio.Queue(maxsize=64)
        self._lock = asyncio.Lock()
        self._closed = False
        self._utterance_active = False

    async def connect(self) -> None:
        async with self._lock:
            if self._ws is not None:
                return

            funasr_token = _funasr_token()
            funasr_url = _funasr_url()
            proxy_url = await asyncio.to_thread(_active_proxy)
            parsed_funasr_url = urlparse(funasr_url)
            if _is_local_host(parsed_funasr_url.hostname):
                proxy_url = None
            resolved_host = None
            if parsed_funasr_url.hostname and not proxy_url:
                resolved_host = await _resolve_funasr_host(
                    parsed_funasr_url.hostname, proxy_url
                )

            try:
                connect_kwargs = {
                    "proxy_url": proxy_url,
                    "additional_headers": {
                        "Authorization": f"Bearer {funasr_token}"
                    },
                    "open_timeout": 10,
                    "close_timeout": 5,
                    "max_size": 2**22,
                }
                if resolved_host:
                    connect_kwargs["host"] = resolved_host
                self._ws_cm = _websockets_connect(funasr_url, **connect_kwargs)
                self._ws = await self._ws_cm.__aenter__()
                self._reader = asyncio.create_task(
                    self._read_loop(), name="funasr-reader"
                )
            except Exception as exc:
                await self._release_connection()
                status_code = _websocket_status_code(exc)
                if status_code in (401, 403):
                    raise AsrAuthError(
                        f"FunASR 拒绝连接(HTTP {status_code}，token 无效或无权限)",
                        status_code,
                    ) from exc
                raise RuntimeError(f"FunASR 连接失败:{exc}") from exc

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._ws.recv()
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    await self._put_event(
                        FunAsrEvent("error", message="FunASR 返回格式错误")
                    )
                    raise RuntimeError("FunASR 返回格式错误") from exc
                msg_type = message.get("type")
                if msg_type == "started":
                    continue
                if msg_type in {"partial", "final"}:
                    text = message.get("text")
                    if not isinstance(text, str):
                        await self._put_event(
                            FunAsrEvent("error", message="FunASR 返回格式错误")
                        )
                        continue
                    await self._put_event(FunAsrEvent(msg_type, text=text.strip()))
                elif msg_type == "error":
                    await self._put_event(
                        FunAsrEvent(
                            "error", message=str(message.get("message", "未知"))
                        )
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 转换为流内结构化错误事件
            await self._put_event(FunAsrEvent("error", message=f"FunASR 连接中断:{exc}"))

    async def _put_event(self, event: FunAsrEvent) -> None:
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            # 只保留最近事件；音频上传不能因为旧 partial 堆积而阻塞。
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._events.put_nowait(event)

    async def _read_for(self, timeout: float) -> list[FunAsrEvent]:
        events: list[FunAsrEvent] = []
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                event = await asyncio.wait_for(self._events.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if event.type == "error":
                raise RuntimeError(f"FunASR 错误:{event.message}")
            events.append(event)
            if event.type == "final":
                break
        self._sweep_ready(events)
        return events

    def _sweep_ready(self, events: list[FunAsrEvent]) -> None:
        """把已经排队的事件全部收走，不等待。"""
        while True:
            try:
                event = self._events.get_nowait()
            except asyncio.QueueEmpty:
                return
            if event.type == "error":
                raise RuntimeError(f"FunASR 错误:{event.message}")
            events.append(event)

    async def _drain_until_idle(
        self, idle_timeout: float, max_wait: float
    ) -> list[FunAsrEvent]:
        """推流后收取累计 partial:拿到就走,最多等 min(idle_timeout, max_wait)。

        推流阶段不能等 final(整段结束前不会有 final),也不能每收一条就再等一个
        空闲窗口 —— 那样每个分片都要付掉一整个 idle_timeout,分片比这个值短的
        时候音频 worker 直接被采集线程甩开,队列填满后背压回踩,首字反而更慢。

        partial 是**累计全文**,所以"最新一条"就够用,不必为"可能更新的下一条"
        守着。真的又来了新 partial,它会在下一个分片推流时被立刻扫走,或者在
        `finish()` 的 `_read_for` 里收尾,不会丢。
        """
        events: list[FunAsrEvent] = []
        # 上一次推流之后网关可能已经吐了新 partial，先白拿，一毫秒都不等。
        self._sweep_ready(events)
        if events:
            return events
        budget = min(idle_timeout, max_wait)
        if budget <= 0:
            return events
        try:
            events.append(await asyncio.wait_for(self._events.get(), timeout=budget))
        except asyncio.TimeoutError:
            return events
        if events[-1].type == "error":
            raise RuntimeError(f"FunASR 错误:{events.pop().message}")
        # 同一时刻队列里可能已经堆了更新的 partial，一并收走再返回。
        self._sweep_ready(events)
        return events

    async def push_wav(
        self, audio_bytes: bytes, declared_duration_ms: int
    ) -> list[FunAsrEvent]:
        """把一个分片推进**当前 utterance**,不结束它。

        `start` 只在语音段的第一个分片发送;后续分片只推裸 PCM。这里**不发
        `stop`**,因此返回值里通常只有 `partial`(当前语音段的累计全文),
        `final` 只会在 `finish()` 里出现。返回空列表是正常情况(网关还没吐
        新的 partial),调用方不得据此判定超时。
        """
        await self.connect()
        async with self._lock:
            if self._closed or self._ws is None:
                raise RuntimeError("FunASR 长连接已关闭")
            # 并发门只覆盖"探测+预算+推流+等 partial"的活跃窗口(对齐
            # _transcribe_funasr 的一次调用一个许可):连接跨语音段复用,
            # 空闲长连接不得占住许可,否则一个 radio_mode=both 会话的
            # 两条流就吃满默认 AI_ASR_MAX_CONCURRENCY=2,第二会话直接
            # PaidCallBusyError。
            async with cost_control.paid_call_slot("asr"):
                actual_duration_ms = await asyncio.to_thread(
                    inspect_audio, audio_bytes, "wav_pcm_s16le", declared_duration_ms
                )
                await cost_control.reserve_asr_millis(
                    _funasr_token(), actual_duration_ms
                )
                if not self._utterance_active:
                    await self._ws.send(
                        json.dumps(
                            {
                                "type": "start",
                                "sample_rate": 16000,
                                "format": "pcm_s16le",
                                "channels": 1,
                            }
                        )
                    )
                    self._utterance_active = True
                await self._ws.send(_wav_payload(audio_bytes))
                events = await self._drain_until_idle(
                    _funasr_partial_idle_seconds(), _funasr_partial_max_wait_seconds()
                )
            if any(event.type == "final" for event in events):
                self._utterance_active = False
            return events

    async def finish(self) -> list[FunAsrEvent]:
        """结束当前 utterance:发一次 `stop` 并等待段末 `final`。"""
        async with self._lock:
            if self._closed or self._ws is None or not self._utterance_active:
                return []
            async with cost_control.paid_call_slot("asr"):
                await self._ws.send(json.dumps({"type": "stop"}))
                events = await self._read_for(
                    float(os.environ.get("AI_FUNASR_FINAL_TIMEOUT_SECONDS", "5"))
                )
            if any(event.type == "final" for event in events):
                self._utterance_active = False
            return events

    async def _release_connection(self) -> None:
        reader = self._reader
        self._reader = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        ws = self._ws
        ws_cm = self._ws_cm
        self._ws = None
        self._ws_cm = None
        self._utterance_active = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001,S110 - 清理路径不得覆盖原始异常
                pass
        if ws_cm is not None:
            try:
                await ws_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001,S110 - 清理路径不得覆盖原始异常
                pass

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._release_connection()


async def _transcribe_funasr(audio_bytes: bytes, declared_duration_ms: int) -> str:
    """兼容性的单分片 FunASR 路径；实时流水线使用 FunAsrStream。"""
    import json as _json

    pcm = _wav_payload(audio_bytes)
    funasr_token = _funasr_token()
    funasr_url = _funasr_url()
    # 代理选择会同步读 SQLite,移出事件循环(对齐 _transcribe_groq)
    proxy_url = await asyncio.to_thread(_active_proxy)
    parsed_funasr_url = urlparse(funasr_url)
    # 同机隧道必须直连；不能把 ws://127.0.0.1:10096 送进系统 HTTP 代理。
    if _is_local_host(parsed_funasr_url.hostname):
        proxy_url = None
    resolved_host = None
    if parsed_funasr_url.hostname and not proxy_url:
        resolved_host = await _resolve_funasr_host(parsed_funasr_url.hostname, proxy_url)
    async with cost_control.paid_call_slot("asr"):
        actual_duration_ms = await asyncio.to_thread(
            inspect_audio, audio_bytes, "wav_pcm_s16le", declared_duration_ms
        )
        await cost_control.reserve_asr_millis(funasr_token, actual_duration_ms)
        try:
            connect_kwargs = {
                "proxy_url": proxy_url,
                "additional_headers": {"Authorization": f"Bearer {funasr_token}"},
                "open_timeout": 10,
                "close_timeout": 5,
                "max_size": 2**22,
            }
            if resolved_host:
                # URI host 仍用于 TLS SNI/Host；host 只覆盖底层 TCP 目标地址。
                connect_kwargs["host"] = resolved_host
            ws_cm = _websockets_connect(funasr_url, **connect_kwargs)
            ws = await ws_cm.__aenter__()
        except Exception as exc:
            status_code = _websocket_status_code(exc)
            if status_code in (401, 403):
                raise AsrAuthError(
                    f"FunASR 拒绝连接(HTTP {status_code}，token 无效或无权限)",
                    status_code,
                ) from exc
            raise RuntimeError(f"FunASR 连接失败:{exc}") from exc
        try:
            await ws.send(
                _json.dumps(
                    {
                        "type": "start",
                        "sample_rate": 16000,
                        "format": "pcm_s16le",
                        "channels": 1,
                    }
                )
            )
            await ws.send(pcm)
            await ws.send(_json.dumps({"type": "stop"}))
            # final 等待上限:分片时长 + 10s 余量
            deadline = asyncio.get_event_loop().time() + max(
                10.0, declared_duration_ms / 1000 + 8.0
            )
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise RuntimeError("FunASR 响应超时")
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                try:
                    message = _json.loads(raw)
                except ValueError as exc:
                    raise RuntimeError("FunASR 返回格式错误") from exc
                msg_type = message.get("type")
                if msg_type == "final":
                    text = message.get("text")
                    if not isinstance(text, str):
                        raise RuntimeError("FunASR 返回格式错误")
                    return text.strip()
                if msg_type == "error":
                    raise RuntimeError(f"FunASR 错误:{message.get('message', '未知')}")
                # started / partial 忽略(分片模型只取 final)
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001,S110 - 清理路径不得覆盖原始异常
                pass


def _groq_request_context(codec: str) -> dict:
    """一次同步读取 Groq 所需的 Key、模型与出网代理配置。

    对齐 llm.py 的做法：这些读取会同步打开 SQLite（busy_timeout=5000），
    必须移出事件循环，否则每次转写都在循环线程里阻塞等锁。
    """
    api_key = _get_api_key()
    data = {"model": _get_model()}
    language = os.environ.get("ASR_LANGUAGE", "zh").strip()
    if language and language.lower() != "auto":
        data["language"] = language
    filename, content_type = CODEC_DETAILS[codec]
    timeout = httpx.Timeout(30.0, connect=10.0)
    return {
        "api_key": api_key,
        "data": data,
        "filename": filename,
        "content_type": content_type,
        "client_kwargs": http_client_kwargs(timeout),
    }


async def _transcribe_groq(
    audio_bytes: bytes, codec: str, declared_duration_ms: int
) -> str:
    """Groq Whisper 回退路径(激活配置 model 以 whisper 开头时使用)。"""
    context = await asyncio.to_thread(_groq_request_context, codec)
    files = {
        "file": (context["filename"], audio_bytes, context["content_type"])
    }

    async with cost_control.paid_call_slot("asr"):
        actual_duration_ms = await asyncio.to_thread(
            inspect_audio, audio_bytes, codec, declared_duration_ms
        )
        await cost_control.reserve_asr_millis(context["api_key"], actual_duration_ms)
        async with httpx.AsyncClient(**context["client_kwargs"]) as client:
            for attempt in range(3):
                resp = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {context['api_key']}"},
                    files=files,
                    data=context["data"],
                )
                if resp.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                    if resp.status_code in (401, 403):
                        raise AsrAuthError(
                            f"转写服务拒绝了请求(HTTP {resp.status_code}，API Key 无效或无权限)",
                            resp.status_code,
                        )
                    resp.raise_for_status()
                    try:
                        result = resp.json()
                    except ValueError as exc:
                        raise RuntimeError("ASR 返回格式错误") from exc
                    if not isinstance(result, dict):
                        raise RuntimeError("ASR 返回格式错误")
                    text = result.get("text")
                    if not isinstance(text, str):
                        raise RuntimeError("ASR 返回格式错误")
                    if len(text) > 20_000:
                        raise RuntimeError("ASR 返回文本过长")
                    return text.strip()
                await asyncio.sleep((2**attempt) * 0.25 + random.random() * 0.1)
    raise RuntimeError("ASR 请求失败")
