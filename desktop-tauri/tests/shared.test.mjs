import assert from 'node:assert/strict'
import test from 'node:test'
import { installWindowStub } from './helpers.mjs'

installWindowStub()

const { errorMessage, emitToast } = await import('../src/shared/errors.ts')

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
