/**
 * node:test 公共测试环境垫片。
 * Node 22 type-stripping 不解析 tsconfig paths,也不带 DOM,统一在此补齐。
 */
import assert from 'node:assert/strict'

// 1) 裸说明符 ./wav-meta → ./wav-meta.ts(recorder.ts 内部相对导入)
//    通过自定义模块解析 hook 重写。
export async function registerTsExtensionResolve() {
  const { register } = await import('node:module')
  const { pathToFileURL } = await import('node:url')
  register(
    new URL('./ts-resolve-hooks.mjs', import.meta.url)
  )
  void pathToFileURL
  void assert
}

// 2) window/Crypto 等 DOM 全局(sessions/settings store 的 toast 依赖 window)
export function installWindowStub() {
  if (globalThis.window) return
  const listeners = new Map()
  globalThis.window = {
    dispatchEvent(event) {
      for (const fn of listeners.get(event.type) ?? []) fn(event)
      return true
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(fn)
    },
    removeEventListener(type, fn) {
      listeners.get(type)?.delete(fn)
    }
  }
}
