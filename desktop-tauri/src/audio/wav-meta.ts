/**
 * WAV (PCM s16le) 封装与时长计算 —— 纯函数，单测覆盖。
 * 后端 ffprobe 要求：format "wav"、codec pcm_s16le、单音轨、
 * 真实时长 = 采样数/采样率（header 中的 dataSize 必须精确）。
 */

export const SAMPLE_RATE = 16_000
/** 每分片目标帧数：48000 帧 = 精确 3000ms */
export const TARGET_FRAMES = 48_000
/** 服务端最短真实时长 100ms = 1600 帧；不足则丢弃 */
export const MIN_FRAMES = 1_600

export function framesToDurationMs(frames: number): number {
  return Math.round((frames * 1000) / SAMPLE_RATE)
}

/** Float32 [-1,1] → int16 LE 钳位转换。 */
export function floatToInt16(samples: Float32Array): Int16Array {
  const out = new Int16Array(samples.length)
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff
  }
  return out
}

/** int16 PCM → 完整自包含 WAV 文件字节（44 字节头 + 数据）。 */
export function encodeWav(pcm: Int16Array, sampleRate = SAMPLE_RATE): ArrayBuffer {
  const dataBytes = pcm.length * 2
  const buffer = new ArrayBuffer(44 + dataBytes)
  const view = new DataView(buffer)

  const writeString = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) {
      view.setUint8(offset + i, text.charCodeAt(i))
    }
  }

  writeString(0, 'RIFF')
  view.setUint32(4, 36 + dataBytes, true) // RIFF chunk size
  writeString(8, 'WAVE')
  writeString(12, 'fmt ')
  view.setUint32(16, 16, true) // fmt chunk size
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true) // byte rate = rate * channels * 2
  view.setUint16(32, 2, true) // block align
  view.setUint16(34, 16, true) // bits per sample
  writeString(36, 'data')
  view.setUint32(40, dataBytes, true)

  const out = new Int16Array(buffer, 44, pcm.length)
  out.set(pcm)
  return buffer
}
