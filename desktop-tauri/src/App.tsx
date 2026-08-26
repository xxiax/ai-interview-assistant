import { useEffect } from 'react'
import { HashRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import {
  History,
  Mic,
  Settings,
  SignalHigh,
  SignalLow,
  SignalMedium,
  Loader2
} from 'lucide-react'
import { api } from './api/bridge'
import { useLiveStore } from './stores/live'
import { Toaster } from './components/ui'
import HomePage from './pages/HomePage'
import LivePage from './pages/LivePage'
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

      {/* 底部:连接状态 */}
      <div className="border-t border-stroke-subtle px-5 py-4">
        <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-ink-faint">
          服务连接
        </div>
        <ConnIndicator />
      </div>
    </aside>
  )
}

// ---------- 布局 ----------

function AppLayout() {
  const location = useLocation()
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

  // /history 路由改为首页内列表(保留路径兼容旧跳转)
  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="min-w-0 flex-1 overflow-hidden bg-surface">
        <Routes location={location}>
          <Route path="/" element={<HomePage mode="active" />} />
          <Route path="/live/:sessionId" element={<LivePage />} />
          <Route path="/history" element={<HomePage mode="history" />} />
          <Route path="/session/:sessionId" element={<SessionDetailPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <Toaster toasts={toasts} onDismiss={dismissToast} />
    </div>
  )
}

export default function App() {
  return (
    <HashRouter>
      <AppLayout />
    </HashRouter>
  )
}
