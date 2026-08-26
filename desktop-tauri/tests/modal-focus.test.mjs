/**
 * 任务 A 回归测试:Modal 焦点管理。
 * 1) 源码断言:effect 依赖不含 onClose(新函数引用导致每字符 re-run 抢焦点);
 * 2) 源码断言:初始聚焦只在 open false→true 边沿执行一次;
 * 3) 源码断言:关闭时还原 previouslyFocused;
 * 4) 行为测试:pickInitialFocusTarget 优先表单控件而非 header 的关闭按钮。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

const uiSource = await readFile(new URL('../src/components/ui.tsx', import.meta.url), 'utf8')
const { pickInitialFocusTarget } = await import('../src/components/modal-focus.ts')

// ---------- 源码断言 ----------

test('Modal: no effect dependency array contains onClose (focus stealing regression)', () => {
  const depArrays = [...uiSource.matchAll(/\}, \[([^\]]*)\]\)/g)].map((m) => m[1])
  assert.ok(depArrays.length > 0, 'Modal should declare effect dependency arrays')
  for (const deps of depArrays) {
    assert.ok(
      !/\bonClose\b/.test(deps),
      `effect dependency array [${deps}] must not contain onClose; ` +
        'callers (SettingsPage closeForm) create a fresh closure each render, ' +
        'which re-runs the effect on every keystroke and steals focus'
    )
  }
})

test('Modal: initial focus runs only on open false→true edge (prevOpenRef guard)', () => {
  assert.match(uiSource, /prevOpenRef/, 'must track previous open state')
  assert.match(uiSource, /if \(open && !wasOpen\)/, 'initial focus must be edge-triggered')
})

test('Modal: focus is restored to the pre-open element when the dialog closes', () => {
  assert.match(uiSource, /previouslyFocusedRef/, 'must remember the element focused before open')
  assert.match(
    uiSource,
    /previouslyFocusedRef\.current\?\.focus\?\.\(\)/,
    'must restore focus on close and on unmount'
  )
})

test('Modal: keydown listener effect depends only on [open], onClose read via ref', () => {
  assert.match(uiSource, /onCloseRef\.current/, 'Esc handler must read onClose from a ref')
  assert.match(
    uiSource,
    /window\.removeEventListener\('keydown', onKey\)\s*\n\s*\}, \[open\]\)/,
    'keydown effect (Esc + Tab trap) must re-run only when open changes'
  )
})

// ---------- pickInitialFocusTarget 行为测试(轻量 DOM 桩) ----------

test('pickInitialFocusTarget: prefers form control over header close button', () => {
  const headerButton = el('button')
  const input = el('input')
  const container = el('div', [headerButton, input])
  assert.equal(pickInitialFocusTarget(container), input)
})

test('pickInitialFocusTarget: falls back to first button when no form control exists', () => {
  const button = el('button')
  const container = el('div', [button])
  assert.equal(pickInitialFocusTarget(container), button)
})

test('pickInitialFocusTarget: returns container itself when nothing focusable', () => {
  const container = el('div', [])
  assert.equal(pickInitialFocusTarget(container), container)
  assert.equal(pickInitialFocusTarget(null), null)
})

/** querySelector 桩:按选择器类型匹配子元素,支持 button:not([disabled]),覆盖纯函数分支。 */
function el(tag, children = []) {
  return {
    tag,
    children,
    isConnected: true,
    querySelector(selector) {
      const wants = selector.split(',').map((s) => s.trim())
      for (const child of children) {
        const match = wants.some(
          (w) =>
            w === child.tag ||
            (w === '[href]' && child.tag === 'a') ||
            (w.startsWith('button:') && child.tag === 'button' && !child.disabled)
        )
        if (match) return child
      }
      return null
    }
  }
}
