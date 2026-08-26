import assert from 'node:assert/strict'
import test from 'node:test'
import { registerTsExtensionResolve } from './helpers.mjs'

await registerTsExtensionResolve()

const { ChunkSequencer, attachTrackEndedListener, MicRecorder } = await import(
  '../src/audio/recorder.ts'
)

test('ChunkSequencer.alloc returns strictly increasing ids', () => {
  const seq = new ChunkSequencer(0)
  const a = seq.alloc()
  const b = seq.alloc()
  const c = seq.alloc()
  assert.equal(a, 0)
  assert.equal(b, 1)
  assert.equal(c, 2)
  assert.equal(seq.current, 3)
})

test('ChunkSequencer.bumpTo prevents seq collision after watermark catch-up', () => {
  // 场景:重连后引擎权威水位为 7,本地已分配到 3 → 不得再发出 4/5/6
  const seq = new ChunkSequencer(3)
  seq.bumpTo(7)
  assert.equal(seq.alloc(), 7)
  assert.equal(seq.alloc(), 8)
})

test('ChunkSequencer.bumpTo never goes backwards', () => {
  const seq = new ChunkSequencer(10)
  seq.bumpTo(4)
  assert.equal(seq.alloc(), 10)
  seq.bumpTo(10)
  assert.equal(seq.alloc(), 11)
})

function fakeTrack() {
  const listeners = new Set()
  return {
    addEventListener(type, fn) {
      assert.equal(type, 'ended')
      listeners.add(fn)
    },
    removeEventListener(type, fn) {
      assert.equal(type, 'ended')
      listeners.delete(fn)
    },
    emitEnded() {
      for (const fn of listeners) fn()
    },
    listenerCount: () => listeners.size
  }
}

test('track ended listener forwards mic-disconnect error', () => {
  const track = fakeTrack()
  const errors = []
  const detach = attachTrackEndedListener(track, (msg) => errors.push(msg))
  assert.equal(track.listenerCount(), 1)
  track.emitEnded()
  assert.deepEqual(errors, ['麦克风设备已断开'])
  detach()
  assert.equal(track.listenerCount(), 0)
  // 移除后不再转发(stop() 自身引发的 ended 不会误报)
  track.emitEnded()
  assert.equal(errors.length, 1)
})

test('MicRecorder.stop resolves and is idempotent', async () => {
  const recorder = new MicRecorder(new ChunkSequencer(0), {
    onChunk: () => {},
    onError: () => {}
  })
  // 未 start:stop 应直接完成且不抛错
  await recorder.stop()
  const again = recorder.stop()
  assert.ok(again instanceof Promise)
  await again
})
