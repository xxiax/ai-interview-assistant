/**
 * 麦克风采集封装:getUserMedia + AudioContext(16kHz) + AudioWorklet。
 * AudioWorklet 会丢弃整段静音/底噪，不为静音分配 UUID 或 chunk_seq。
 * 产出的分片经 wav-meta 编码后通过 Tauri 命令发往 Rust outbox。
 */
import {
  MIN_FRAMES,
  SAMPLE_RATE,
  TARGET_FRAMES,
  encodeWav,
  framesToDurationMs
} from './wav-meta'

export interface RecordedChunk {
  chunkId: string
  chunkSeq: number
  capturedAt: string
  durationMs: number
  data: ArrayBuffer
}

export interface RecorderCallbacks {
  onChunk: (chunk: RecordedChunk) => void
  onError: (message: string) => void
}

/** 是否为浏览器环境中的 MediaStreamTrack(测试里用鸭子类型 mock)。 */
interface TrackLike {
  addEventListener: (type: string, listener: () => void) => void
  removeEventListener: (type: string, listener: () => void) => void
}

/**
 * 给 track 挂 ended 监听;设备拔出/蓝牙断连时 track 会静默 ended。
 * 返回移除函数(Recorder.stop 时调用,避免 stop() 自身触发误报)。
 */
export function attachTrackEndedListener(track: TrackLike, onError: (message: string) => void): () => void {
  const listener = () => onError('麦克风设备已断开')
  track.addEventListener('ended', listener)
  return () => track.removeEventListener('ended', listener)
}

/** 分片序号分配器:由调用方注入当前值(重连/重启后从对账结果恢复)。 */
export class ChunkSequencer {
  private next: number
  constructor(start: number) {
    this.next = start
  }
  alloc(): number {
    return this.next++
  }
  bumpTo(atLeast: number): void {
    this.next = Math.max(this.next, atLeast)
  }
  get current(): number {
    return this.next
  }
}

export class MicRecorder {
  private stream: MediaStream | null = null
  private context: AudioContext | null = null
  private node: AudioWorkletNode | null = null
  private sequencer: ChunkSequencer
  private callbacks: RecorderCallbacks
  /** context.currentTime → wall clock 的锚点(captured_at 换算) */
  private timeAnchor: { contextTime: number; wallMs: number } | null = null
  /** track ended 监听移除函数(与 stream 生命周期一致) */
  private detachTrackEnded: (() => void)[] = []
  /** stop() 完成(尾分片已发出、资源已清理)时 resolve。 */
  private stoppedPromise: Promise<void> | null = null

  constructor(sequencer: ChunkSequencer, callbacks: RecorderCallbacks) {
    this.sequencer = sequencer
    this.callbacks = callbacks
  }

  get active(): boolean {
    return this.node !== null
  }

  async start(): Promise<void> {
    if (this.node) return
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          // 电脑播放声由 WASAPI loopback 单独采集。麦克风必须启用 AEC/降噪，
          // 否则扬声器里的对方声音会再从麦克风漏入，产生重复转写和底噪分片。
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1
        }
      })
    } catch (err) {
      this.cleanup()
      throw new Error(`无法访问麦克风:${(err as Error).message}`)
    }
    // 设备拔出/蓝牙断连时 track 会静默 ended;转发给调用方降级处理。
    for (const track of this.stream.getTracks()) {
      this.detachTrackEnded.push(attachTrackEndedListener(track, this.callbacks.onError))
    }

    let context: AudioContext
    try {
      context = new AudioContext({ sampleRate: SAMPLE_RATE })
    } catch {
      context = new AudioContext()
    }
    this.context = context
    if (context.state === 'suspended') {
      await context.resume()
    }

    try {
      await context.audioWorklet.addModule('/worklets/wav-worklet.js')
    } catch (err) {
      this.cleanup()
      throw new Error(`音频模块加载失败:${(err as Error).message}`)
    }

    const source = context.createMediaStreamSource(this.stream)
    const node = new AudioWorkletNode(context, 'wav-recorder', {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      processorOptions: {
        targetSampleRate: SAMPLE_RATE,
        targetFrames: TARGET_FRAMES,
        minFrames: MIN_FRAMES
      }
    })
    this.node = node
    this.timeAnchor = { contextTime: context.currentTime, wallMs: Date.now() }

    node.port.onmessage = (event: MessageEvent) => {
      const data = event.data as {
        type: string
        int16: ArrayBuffer
        frames: number
        startContextTime: number
      }
      if (data.type !== 'chunk' || !this.timeAnchor) return
      const wallMs =
        this.timeAnchor.wallMs + (data.startContextTime - this.timeAnchor.contextTime) * 1000
      const wav = encodeWav(new Int16Array(data.int16))
      this.callbacks.onChunk({
        chunkId: crypto.randomUUID(),
        chunkSeq: this.sequencer.alloc(),
        capturedAt: new Date(wallMs).toISOString(),
        durationMs: framesToDurationMs(data.frames),
        data: wav
      })
    }

    source.connect(node)
  }

  /**
   * 停止采集。返回的 promise 在尾分片(若触发)已回调、资源已清理后 resolve,
   * 调用方据此再失效回调守卫,避免丢弃最多 3 秒的末尾语音。
   */
  stop(): Promise<void> {
    if (this.stoppedPromise) return this.stoppedPromise
    if (this.node) {
      this.node.port.postMessage({ type: 'stop' })
      // 尾部分片(若触发)会在消息回调里发出;短暂等待后清理
      this.stoppedPromise = new Promise<void>((resolve) => {
        setTimeout(() => {
          this.cleanup()
          resolve()
        }, 200)
      })
    } else {
      this.cleanup()
      this.stoppedPromise = Promise.resolve()
    }
    return this.stoppedPromise
  }

  private cleanup(): void {
    for (const detach of this.detachTrackEnded.splice(0)) detach()
    this.node?.disconnect()
    this.node = null
    this.stream?.getTracks().forEach((t) => t.stop())
    this.stream = null
    void this.context?.close().catch(() => {})
    this.context = null
    this.timeAnchor = null
  }
}
