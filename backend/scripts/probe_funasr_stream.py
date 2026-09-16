"""一次性探测脚本:验证 FunASR 网关的开放式 utterance 语义。

不属于产品代码,只用于回答三个问题:
  A. 一次 start 之后连续推多个切片、中途不发 stop,partial 是否持续返回?
  B. partial 文本是"当前句累计全文"还是"只有当前切片"?
  C. 与旧版"每片 start/stop"相比,整段单 utterance 的转写准确率差多少?

用法:
    python scripts/probe_funasr_stream.py <wav 路径>

依赖运行环境已有的 websockets 与 backend/.env(读取 AI_FUNASR_URL / AI_FUNASR_TOKEN)。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import wave
from pathlib import Path

import websockets

BACKEND_DIR = Path(__file__).resolve().parents[1]
# 与客户端 AI_AUDIO_CHUNK_MS 的默认值保持一致，探针才代表真实分片节奏。
SLICE_MS = int(os.environ.get("AI_AUDIO_CHUNK_MS", "400"))
SAMPLE_RATE = 16_000


def load_env() -> None:
    env_path = BACKEND_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def read_slices(path: Path) -> list[bytes]:
    with wave.open(str(path)) as wav:
        assert wav.getnchannels() == 1, "需要单声道"
        assert wav.getframerate() == SAMPLE_RATE, "需要 16 kHz"
        assert wav.getsampwidth() == 2, "需要 s16le"
        pcm = wav.readframes(wav.getnframes())
    frames_per_slice = SAMPLE_RATE * SLICE_MS // 1_000
    step = frames_per_slice * 2
    return [pcm[i : i + step] for i in range(0, len(pcm), step)]


def connect(url: str, token: str):
    return websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=10,
        close_timeout=5,
        max_size=2**22,
    )


async def drain(ws, seconds: float) -> list[dict]:
    out: list[dict] = []
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return out
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return out
        out.append(json.loads(raw))


async def probe_open_utterance(url: str, token: str, slices: list[bytes]) -> None:
    print("=" * 74)
    print("探测 A/B:一次 start,连续推所有切片,中途不发 stop")
    print("=" * 74)
    async with connect(url, token) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "start",
                    "sample_rate": SAMPLE_RATE,
                    "format": "pcm_s16le",
                    "channels": 1,
                }
            )
        )
        for message in await drain(ws, 2.0):
            print(f"  <- {message}")

        for index, chunk in enumerate(slices):
            await ws.send(chunk)
            print(f"\n  -> 切片 {index} 已推送 ({len(chunk)} bytes)，不发 stop")
            for message in await drain(ws, 3.0):
                kind = message.get("type")
                text = message.get("text", "")
                print(f"  <- {kind:8s} text={text!r} audio_ms={message.get('audio_ms')}")

        print("\n  -> 所有切片推完，现在才发 stop")
        await ws.send(json.dumps({"type": "stop"}))
        for message in await drain(ws, 8.0):
            kind = message.get("type")
            print(f"  <- {kind:8s} text={message.get('text')!r}")


async def probe_per_slice(url: str, token: str, slices: list[bytes]) -> None:
    print()
    print("=" * 74)
    print("对照 C:旧版逐片模式,每片自己 start/PCM/stop 并等 final")
    print("=" * 74)
    finals: list[str] = []
    async with connect(url, token) as ws:
        for index, chunk in enumerate(slices):
            await ws.send(
                json.dumps(
                    {
                        "type": "start",
                        "sample_rate": SAMPLE_RATE,
                        "format": "pcm_s16le",
                        "channels": 1,
                    }
                )
            )
            await ws.send(chunk)
            await ws.send(json.dumps({"type": "stop"}))
            for message in await drain(ws, 6.0):
                if message.get("type") == "final":
                    finals.append(message.get("text", ""))
                    print(f"  切片 {index} final = {message.get('text')!r}")
    print(f"\n  按片拼接结果 = {''.join(finals)!r}")


async def main() -> int:
    load_env()
    url = os.environ.get("AI_FUNASR_URL", "ws://127.0.0.1:10096/ws")
    token = os.environ.get("AI_FUNASR_TOKEN", "")
    if not token:
        print("缺少 AI_FUNASR_TOKEN", file=sys.stderr)
        return 2
    wav_path = Path(sys.argv[1] if len(sys.argv) > 1 else "")
    if not wav_path.exists():
        print(f"WAV 不存在: {wav_path}", file=sys.stderr)
        return 2

    slices = read_slices(wav_path)
    print(f"地址={url}  切片数={len(slices)}  每片={SLICE_MS}ms\n")
    await probe_open_utterance(url, token, slices)
    await probe_per_slice(url, token, slices)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
