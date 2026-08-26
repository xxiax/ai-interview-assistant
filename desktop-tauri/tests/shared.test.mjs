import assert from 'node:assert/strict'
import test from 'node:test'
import { installWindowStub } from './helpers.mjs'

installWindowStub()

const { errorMessage, emitToast } = await import('../src/shared/errors.ts')
const { encodeWav, floatToInt16, framesToDurationMs, SAMPLE_RATE } = await import(
  '../src/audio/wav-meta.ts'
)

// ---------- errorMessage ----------

test('errorMessage: Error 对象取 message', () => {
  assert.equal(errorMessage(new Error('boom')), 'boom')
})

test('errorMessage: 字符串直接返回(Tauri invoke 拒绝常见形态)', () => {
  assert.equal(errorMessage('command failed'), 'command failed')
})

test('errorMessage: 带 message 字符串的对象取 message', () => {
  assert.equal(errorMessage({ message: 'network down' }), 'network down')
})

test('errorMessage: 空 message 的 Error 与空串对象不产生空 toast', () => {
  assert.equal(errorMessage(new Error('')), 'Error')
  assert.equal(errorMessage({ message: '' }), String({ message: '' }))
})

test('errorMessage: undefined/null 兜底为字符串', () => {
  assert.equal(errorMessage(undefined), 'undefined')
  assert.equal(errorMessage(null), 'null')
  assert.equal(errorMessage(0), '0')
})

test('emitToast dispatches app-toast event', () => {
  const seen = []
  const listener = (e) => seen.push(e.detail)
  window.addEventListener('app-toast', listener)
  emitToast('error', 'x')
  window.removeEventListener('app-toast', listener)
  assert.deepEqual(seen, [{ kind: 'error', message: 'x' }])
})

// ---------- encodeWav / floatToInt16 ----------

test('floatToInt16: 正负满幅与削波边界', () => {
  const out = floatToInt16(new Float32Array([1, -1, 0.5, -0.5, 2, -2, 0]))
  assert.equal(out[0], 0x7fff)
  assert.equal(out[1], -0x8000)
  // 0.5 * 0x7fff = 16383.5 → Int16Array 赋值截断为 16383(非四舍五入)
  assert.equal(out[2], Math.trunc(0.5 * 0x7fff))
  assert.equal(out[3], Math.trunc(-0.5 * 0x8000))
  assert.equal(out[4], 0x7fff) // 削波到 +1
  assert.equal(out[5], -0x8000) // 削波到 -1
  assert.equal(out[6], 0)
})

test('floatToInt16: 奇数长度样本完整转换', () => {
  const input = new Float32Array([0.1, -0.2, 0.3, -0.4, 0.5])
  const out = floatToInt16(input)
  assert.equal(out.length, 5)
  assert.ok(out instanceof Int16Array)
})

test('encodeWav: 44 字节头字段与数据精确(奇数样本)', () => {
  const pcm = Int16Array.from([1, -2, 3])
  const buf = encodeWav(pcm)
  const view = new DataView(buf)
  assert.equal(buf.byteLength, 44 + pcm.length * 2)
  const ascii = (offset, len) => String.fromCharCode(...new Uint8Array(buf, offset, len))
  assert.equal(ascii(0, 4), 'RIFF')
  assert.equal(view.getUint32(4, true), 36 + pcm.length * 2)
  assert.equal(ascii(8, 4), 'WAVE')
  assert.equal(ascii(12, 4), 'fmt ')
  assert.equal(view.getUint32(16, true), 16)
  assert.equal(view.getUint16(20, true), 1) // PCM
  assert.equal(view.getUint16(22, true), 1) // mono
  assert.equal(view.getUint32(24, true), SAMPLE_RATE)
  assert.equal(view.getUint32(28, true), SAMPLE_RATE * 2)
  assert.equal(view.getUint16(32, true), 2)
  assert.equal(view.getUint16(34, true), 16)
  assert.equal(ascii(36, 4), 'data')
  assert.equal(view.getUint32(40, true), pcm.length * 2)
  assert.deepEqual(new Int16Array(buf, 44, pcm.length), pcm)
})

test('encodeWav: 空数据仍是合法头', () => {
  const buf = encodeWav(new Int16Array(0))
  assert.equal(buf.byteLength, 44)
  assert.equal(new DataView(buf).getUint32(40, true), 0)
})

test('framesToDurationMs: 3 秒分片与舍入', () => {
  assert.equal(framesToDurationMs(48_000), 3000)
  assert.equal(framesToDurationMs(1_600), 100)
  assert.equal(framesToDurationMs(0), 0)
})
