/**
 * WAV 采集 AudioWorklet 处理器。
 * 累积 Float32 帧，满 TARGET_FRAMES(48000=3000ms) 时 postMessage 完整分片的
 * int16 数据（转移所有权）。停止时剩余帧 ≥ MIN_FRAMES 才冲刷。
 *
 * 采样率说明：AudioContext 以 {sampleRate: 16000} 创建；若设备不支持，
 * 浏览器会在上下文内部重采样，worklet 收到的输入即为 16kHz。
 * 兜底：若实际 context 采样率非 16000（极端情况），在 worklet 内线性降采样。
 */
class WavRecorderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const opts = options && options.processorOptions ? options.processorOptions : {}
    this.targetSampleRate = opts.targetSampleRate || 16000
    this.targetFrames = opts.targetFrames || 48000
    this.minFrames = opts.minFrames || 1600
    this.ratio = sampleRate / this.targetSampleRate // context rate / target rate
    this.buffer = new Float32Array(this.targetFrames)
    this.fill = 0
    this.stopped = false
    // 20ms 能量窗。连续至少 60ms 高于底噪阈值才视为有效声音，避免无人说话时
    // 仍每 3 秒创建一个 WAV 并消耗 ASR/重试队列。
    this.activityFrameSize = opts.activityFrameSize || Math.round(this.targetSampleRate * 0.02)
    this.activityRmsThreshold = opts.activityRmsThreshold || 0.002
    this.activityMinFrames = opts.activityMinFrames || 3
    /** chunk 起始时间戳（context 时钟 → wall clock 换算由主线程完成） */
    this.chunkStartContextTime = null
    this.port.onmessage = (event) => {
      if (event.data && event.data.type === 'stop') {
        this.handleStop()
      }
    }
  }

  /** 线性降采样 context rate → 16kHz（ratio > 1 时）。 */
  resample(input) {
    if (this.ratio === 1) return input
    const outLen = Math.floor(input.length / this.ratio)
    const out = new Float32Array(outLen)
    for (let i = 0; i < outLen; i++) {
      const pos = i * this.ratio
      const idx = Math.floor(pos)
      const frac = pos - idx
      const a = input[idx] || 0
      const b = input[idx + 1] || 0
      out[i] = a + (b - a) * frac
    }
    return out
  }

  process(inputs) {
    if (this.stopped) return false
    const channel = inputs[0] && inputs[0][0]
    if (!channel || channel.length === 0) return true

    if (this.chunkStartContextTime === null) {
      this.chunkStartContextTime = currentTime
    }

    let samples = this.resample(channel)
    let offset = 0
    while (offset < samples.length) {
      const space = this.targetFrames - this.fill
      const take = Math.min(space, samples.length - offset)
      this.buffer.set(samples.subarray(offset, offset + take), this.fill)
      this.fill += take
      offset += take
      if (this.fill === this.targetFrames) {
        this.flush(false)
      }
    }
    return true
  }

  flush(final) {
    if (this.fill < this.minFrames) {
      // 尾部不足 100ms：丢弃
      this.fill = 0
      this.chunkStartContextTime = null
      return
    }
    const used = this.buffer.subarray(0, this.fill)
    if (!this.hasAudibleSpeech(used)) {
      this.port.postMessage({
        type: 'silence',
        frames: this.fill,
        startContextTime: this.chunkStartContextTime ?? currentTime
      })
      this.fill = 0
      this.chunkStartContextTime = null
      return
    }
    // int16 转换（与 wav-meta.ts floatToInt16 一致）
    const int16 = new Int16Array(this.fill)
    for (let i = 0; i < this.fill; i++) {
      const s = Math.max(-1, Math.min(1, used[i]))
      int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff
    }
    const startContextTime = this.chunkStartContextTime ?? currentTime
    this.port.postMessage(
      {
        type: 'chunk',
        int16: int16.buffer,
        frames: this.fill,
        startContextTime
      },
      [int16.buffer]
    )
    this.fill = 0
    this.chunkStartContextTime = null
  }

  hasAudibleSpeech(samples) {
    let activeFrames = 0
    for (let offset = 0; offset < samples.length; offset += this.activityFrameSize) {
      const end = Math.min(samples.length, offset + this.activityFrameSize)
      let sumSquares = 0
      for (let i = offset; i < end; i++) {
        sumSquares += samples[i] * samples[i]
      }
      const rms = Math.sqrt(sumSquares / Math.max(1, end - offset))
      if (rms >= this.activityRmsThreshold) {
        activeFrames += 1
        if (activeFrames >= this.activityMinFrames) return true
      }
    }
    return false
  }

  /** 主线程通过 port 通知停止并冲刷尾部。 */
  handleStop() {
    this.stopped = true
    this.flush(true)
  }
}

registerProcessor('wav-recorder', WavRecorderProcessor)
