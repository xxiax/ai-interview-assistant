/**
 * 悬浮提词窗控制。主窗口的开关面板和悬浮窗自己都用这一份逻辑。
 *
 * 权威状态在 Rust（`OverlayStateHandle`）：全局热键也会改它，
 * 所以这里既要在动作返回值里更新，也要订阅 `overlay:state` 事件，
 * 否则用热键切了穿透之后界面上的开关还显示旧值。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/bridge'
import { errorMessage } from './errors'
import type { OverlayState } from './types'

/** 与 Rust `OVERLAY_SHORTCUTS` 一一对应（含顺序）；改一边必须改另一边。 */
export const OVERLAY_HOTKEYS: { keys: string; label: string }[] = [
  { keys: 'Ctrl+Alt+O', label: '显示 / 隐藏' },
  { keys: 'Ctrl+Alt+P', label: '鼠标穿透' },
  { keys: 'Ctrl+Alt+S', label: '共享隐身' },
  { keys: 'Ctrl+Alt+=', label: '更清晰' },
  { keys: 'Ctrl+Alt+-', label: '更透明' },
  { keys: 'Ctrl+Alt+Q', label: '截图解题' },
  { keys: 'Ctrl+Alt+E', label: '收起 / 展开' },
  { keys: 'Ctrl+Alt+Z', label: '开启/暂停录制' }
]

/** Rust 端的默认值；首次 snapshot 返回前用它渲染，避免开关闪一下。 */
const FALLBACK_STATE: OverlayState = {
  visible: false,
  focused: false,
  passthrough: false,
  ctrlInteractive: false,
  contentProtected: true,
  alwaysOnTop: true,
  opacity: 0.92,
  collapsed: false
}

function toast(kind: 'error' | 'warning' | 'info' | 'success', message: string) {
  window.dispatchEvent(new CustomEvent('app-toast', { detail: { kind, message } }))
}

export function useOverlayControl() {
  const [state, setState] = useState<OverlayState>(FALLBACK_STATE)
  const [busy, setBusy] = useState(false)
  const aliveRef = useRef(true)

  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

  // 初始快照 + 热键广播订阅。
  useEffect(() => {
    let disposed = false
    let unlisten: (() => void) | undefined
    void api.overlay
      .snapshot()
      .then((next) => {
        if (!disposed) setState(next)
      })
      .catch(() => {
        // 拿不到快照就用默认值渲染，不打扰用户：悬浮窗是可选功能。
      })
    void api.overlay
      .onState((next) => {
        if (!disposed) setState(next)
      })
      .then((fn) => {
        // StrictMode 下 listen 可能在 cleanup 之后才 resolve，补偿解绑。
        if (disposed) fn()
        else unlisten = fn
      })
    return () => {
      disposed = true
      unlisten?.()
    }
  }, [])

  const run = useCallback(async (action: () => Promise<OverlayState>) => {
    setBusy(true)
    try {
      const next = await action()
      if (aliveRef.current) setState(next)
      return next
    } catch (err) {
      toast('error', errorMessage(err))
      return undefined
    } finally {
      if (aliveRef.current) setBusy(false)
    }
  }, [])

  const actions = {
    show: () => run(() => api.overlay.show()),
    hide: () => run(() => api.overlay.hide()),
    toggle: () => run(() => api.overlay.toggle()),
    setPassthrough: (on: boolean) => run(() => api.overlay.setPassthrough(on)),
    setContentProtected: (on: boolean) => run(() => api.overlay.setContentProtected(on)),
    setAlwaysOnTop: (on: boolean) => run(() => api.overlay.setAlwaysOnTop(on)),
    setOpacity: (value: number) => run(() => api.overlay.setOpacity(value)),
    collapse: () => run(() => api.overlay.collapse()),
    expand: () => run(() => api.overlay.expand())
  }

  return { state, busy, actions }
}
