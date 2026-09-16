import { useEffect } from 'react'
import {
  HashRouter,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate
} from 'react-router-dom'
import {
  History,
  Layers,
  Mic,
  Settings,
  SignalHigh,
  SignalLow,
  SignalMedium,
  Loader2
} from 'lucide-react'
import { api } from './api/bridge'
import { useLiveStore } from './stores/live'
import { useOverlayControl } from './shared/overlay-control'
import { Toaster } from './components/ui'
import HomePage from './pages/HomePage'
import LivePage from './pages/LivePage'
import OverlayPage from './pages/OverlayPage'
import SessionDetailPage from './pages/SessionDetailPage'
import SettingsPage from './pages/SettingsPage'

/** toast 自增序号(页面级事件用) */
let toastSeq = 0

// ---------- 连接状态指示器 ----------

interface PhaseView {
  icon: typeof SignalHigh
  color: string
  label: string
  spin?: boolean
}

const PHASE_SIGNAL: Record<string, PhaseView> = {
  idle: { icon: SignalLow, color: 'text-ink-faint', label: '未连接' },
  connecting: { icon: Loader2, color: 'text-warn', label: '连接中', spin: true },
  authenticating: { icon: Loader2, color: 'text-warn', label: '认证中', spin: true },
  synchronizing: { icon: Loader2, color: 'text-warn', label: '同步中', spin: true },
  ready: { icon: SignalHigh, color: 'text-good', label: '已连接' },
  reconnecting: { icon: SignalMedium, color: 'text-warn', label: '重连中' },
  closed: { icon: SignalLow, color: 'text-bad', label: '已断开' }
}

function ConnIndicator() {
  const phase = useLiveStore((s) => s.phase)
  const view = PHASE_SIGNAL[phase] ?? PHASE_SIGNAL.idle
  const Icon = view.icon
  return (
    <div className="flex items-center gap-1.5" title={`连接状态:${view.label}`}>
      <Icon size={13} className={`${view.color}${view.spin ? ' animate-spin' : ''}`} />
      <span className="text-[11px] text-ink-muted">{view.label}</span>
    </div>
  )
}

// ---------- 侧边栏 ----------

function Sidebar() {
  const navigate = useNavigate()
  const location = useLocation()
  const outboxQueued = useLiveStore((s) => s.outbox?.queued ?? 0)
  const recording = useLiveStore((s) => s.sessionStatus === 'recording')

  const isLive = location.pathname === '/' || location.pathname.startsWith('/live')
  const isHistory = location.pathname.startsWith('/session') || location.pathname === '/history'
  const isSettings = location.pathname.startsWith('/settings')

  const items = [
    {
      key: '/',
      active: isLive,
      icon: Mic,
      label: '面试',
      // 待确认分片数(录制期间网络积压时>0)——属于进行中的面试,不属于历史
      hint: recording ? 'recording' : outboxQueued > 0 ? String(outboxQueued) : null
    },
    {
      key: '/history',
      active: isHistory,
      icon: History,
      label: '历史',
      hint: null
    },
    { key: '/settings', active: isSettings, icon: Settings, label: '设置', hint: null }
  ]

  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-stroke bg-surface-raised">
      {/* 品牌区 */}
      <div className="flex items-center gap-3 px-5 pb-5 pt-6">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-brand to-brand-dim shadow-lg shadow-brand/20">
          <Mic size={18} className="text-white" strokeWidth={2.2} />
        </div>
        <div className="min-w-0">
          <div className="truncate text-[15px] font-semibold tracking-tight text-ink-primary">
            AI 面试助手
          </div>
          <div className="text-[11px] text-ink-faint">Realtime Copilot</div>
        </div>
      </div>

      {/* 导航 */}
      <nav className="flex-1 space-y-1 px-3" aria-label="主导航">
        {items.map((item) => (
          <button
            key={item.key}
            onClick={() => navigate(item.key)}
            aria-current={item.active ? 'page' : undefined}
            className={`group relative flex w-full cursor-pointer items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-all duration-150 ${
              item.active
                ? 'bg-surface-active text-ink-primary'
                : 'text-ink-secondary hover:bg-surface-hover hover:text-ink-primary'
            }`}
          >
            {item.active && (
              <span className="absolute left-0 h-5 w-[3px] rounded-r bg-brand" aria-hidden />
            )}
            <item.icon
              size={17}
              strokeWidth={item.active ? 2.2 : 1.8}
              className={item.active ? 'text-brand' : 'text-ink-muted group-hover:text-ink-secondary'}
            />
            <span className="flex-1 text-left font-medium">{item.label}</span>
            {item.hint === 'recording' && (
              <span className="flex items-center gap-1.5 rounded-full bg-bad/15 px-2 py-0.5 text-[10px] font-medium text-bad">
                <span className="h-1.5 w-1.5 animate-pulse-dot rounded-full bg-bad" />
                LIVE
              </span>
            )}
            {item.hint && item.hint !== 'recording' && (
              <span className="tnum rounded-full bg-brand/15 px-2 py-0.5 text-[10px] font-semibold text-brand">
                {item.hint}
              </span>
            )}
          </button>
        ))}
      </nav>

      {/* 底部:悬浮窗开关 + 连接状态 */}
      <div className="space-y-3 border-t border-stroke-subtle px-5 py-4">
        <OverlayToggle />
        <div>
          <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-ink-faint">
            服务连接
          </div>
          <ConnIndicator />
        </div>
      </div>
    </aside>
  )
}

/**
 * 侧边栏里的悬浮窗开关。
 *
 * 为什么放这里：悬浮窗以前只能从 LivePage 顶栏的弹窗里打开，也就是必须先建会话
 * 再进面试页才能发现它。它是全程可用的功能（会话之前就可以摆好位置和透明度），
 * 入口必须在常驻的侧边栏上。详细开关仍在面试页的弹窗里，这里只管显隐。
 */
function OverlayToggle() {
  const { state, busy, actions } = useOverlayControl()
  return (
    <button
      type="button"
      onClick={() => void actions.toggle()}
      disabled={busy}
      aria-pressed={state.visible}
      title="悬浮提词窗：盖在会议窗口之上显示答案，对系统录屏/共享隐身（Ctrl+Alt+O）"
      className={`flex w-full cursor-pointer items-center gap-2.5 rounded-lg border px-3 py-2 text-[12px] transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
        state.visible
          ? 'border-brand/40 bg-brand/12 text-brand'
          : 'border-stroke bg-surface text-ink-secondary hover:border-brand/35 hover:text-ink-primary'
      }`}
    >
      <Layers size={15} strokeWidth={state.visible ? 2.2 : 1.8} />
      <span className="flex-1 text-left font-medium">悬浮提词窗</span>
      <kbd className="tnum shrink-0 rounded bg-white/10 px-1.5 py-px text-[9px] text-ink-faint">
        Ctrl+Alt+O
      </kbd>
    </button>
  )
}

// ---------- 布局 ----------

function AppLayout() {
  const toasts = useLiveStore((s) => s.toasts)
  const dismissToast = useLiveStore((s) => s.dismissToast)

  // 全局订阅引擎事件(驱动侧边栏状态与错误 toast)
  useEffect(() => {
    let disposed = false
    let unlisten: (() => void) | undefined
    void api.events.on((event) => {
      useLiveStore.getState().applyGlobal(event)
    }).then((fn) => {
      if (disposed) {
        // StrictMode 下 listen 未完成即 cleanup:promise 晚到时补偿解绑
        fn()
        return
      }
      unlisten = fn
    })
    return () => {
      disposed = true
      unlisten?.()
    }
  }, [])

  // 页面级 toast 事件(app-toast CustomEvent)
  useEffect(() => {
    const onToast = (e: Event) => {
      const detail = (e as CustomEvent<{ kind: string; message: string }>).detail
      const store = useLiveStore.getState()
      const id = ++toastSeq
      useLiveStore.setState((s) => ({
        toasts: [...s.toasts, { id, kind: detail.kind, message: detail.message }]
      }))
      setTimeout(() => store.dismissToast(id), 4500)
    }
    window.addEventListener('app-toast', onToast)
    return () => window.removeEventListener('app-toast', onToast)
  }, [])

  // Rust 侧提示(悬浮窗热键失败等)转成同一个 CustomEvent,复用上面那条出口
  useEffect(() => {
    let disposed = false
    let unlisten: (() => void) | undefined
    void api.events
      .onToast((payload) => {
        if (disposed) return
        window.dispatchEvent(new CustomEvent('app-toast', { detail: payload }))
      })
      .then((fn) => {
        if (disposed) fn()
        else unlisten = fn
      })
    return () => {
      disposed = true
      unlisten?.()
    }
  }, [])

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="min-w-0 flex-1 overflow-hidden bg-surface">
        <Outlet />
      </main>
      <Toaster toasts={toasts} onDismiss={dismissToast} />
    </div>
  )
}

export default function App() {
  return (
    <HashRouter>
      <Routes>
        {/*
          悬浮提词窗走独立顶层路由，不套 AppLayout：它不能有侧边栏和 main 容器，
          也不该复用 AppLayout 里的全局订阅（那份订阅驱动的是主窗口的 Zustand
          store，悬浮窗有自己的 reducer）。
        */}
        <Route path="/overlay" element={<OverlayPage />} />
        {/* /history 保留路径兼容旧跳转，实际渲染首页内列表 */}
        <Route element={<AppLayout />}>
          <Route path="/" element={<HomePage mode="active" />} />
          <Route path="/live/:sessionId" element={<LivePage />} />
          <Route path="/history" element={<HomePage mode="history" />} />
          <Route path="/session/:sessionId" element={<SessionDetailPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </HashRouter>
  )
}
