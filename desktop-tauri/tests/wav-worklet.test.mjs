import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

let Processor
let messages

globalThis.sampleRate = 16_000
globalThis.currentTime = 0
globalThis.AudioWorkletProcessor = class {
  constructor() {
    this.port = {
      onmessage: null,
      postMessage(message) {
        messages.push(message)
      }
    }
  }
}
globalThis.registerProcessor = (_name, constructor) => {
  Processor = constructor
}

await import('../public/worklets/wav-worklet.js')

function runChunk(amplitude) {
  messages = []
  const processor = new Processor({
    processorOptions: {
      targetSampleRate: 16_000,
      targetFrames: 48_000,
      minFrames: 1_600
    }
  })
  for (let offset = 0; offset < 48_000; offset += 128) {
    const frame = new Float32Array(Math.min(128, 48_000 - offset))
    for (let index = 0; index < frame.length; index++) {
      frame[index] = Math.sin((offset + index) / 8) * amplitude
    }
    globalThis.currentTime = offset / 16_000
    processor.process([[frame]])
  }
  return messages
}

test('drops microphone noise floor without creating an audio chunk', () => {
  const emitted = runChunk(0.0008)
  assert.equal(emitted.some((message) => message.type === 'chunk'), false)
  assert.equal(emitted.filter((message) => message.type === 'silence').length, 1)
})

test('keeps audible speech frames', () => {
  const emitted = runChunk(0.03)
  assert.equal(emitted.filter((message) => message.type === 'chunk').length, 1)
  assert.equal(emitted.some((message) => message.type === 'silence'), false)
})

test('microphone enables echo cancellation and noise suppression', async () => {
  const recorderSource = await readFile(
    new URL('../src/audio/recorder.ts', import.meta.url),
    'utf8'
  )
  assert.match(recorderSource, /echoCancellation:\s*true/)
  assert.match(recorderSource, /noiseSuppression:\s*true/)
  assert.doesNotMatch(recorderSource, /echoCancellation:\s*false/)
  assert.doesNotMatch(recorderSource, /noiseSuppression:\s*false/)
})

test('live page preserves string errors returned by Tauri commands', async () => {
  const livePageSource = await readFile(
    new URL('../src/pages/LivePage.tsx', import.meta.url),
    'utf8'
  )
  // errorMessage 已提取到 shared/errors 并被 LivePage 引用
  assert.match(livePageSource, /import \{ errorMessage \} from '\.\.\/shared\/errors'/)
  assert.doesNotMatch(livePageSource, /\(err as Error\)\.message/)
})

test('worklet flush has no redundant chunkStartContextTime reset', async () => {
  const workletSource = await readFile(
    new URL('../public/worklets/wav-worklet.js', import.meta.url),
    'utf8'
  )
  // flush 末尾的 if (!final) 二次置 null 是冗余赋值,已删
  assert.doesNotMatch(workletSource, /if \(!final\)/)
})
