/**
 * 共享 UI 基元:按钮/输入/弹窗/toast/空态/徽标。
 * 全部键盘可达,焦点环统一(:focus-visible,见 global.css)。
 */
import { useEffect, useRef, type ReactNode } from 'react'
import { Loader2, X, CheckCircle2, AlertTriangle, XCircle, Inbox } from 'lucide-react'
import { pickInitialFocusTarget } from './modal-focus'

// ---------- Button ----------

type ButtonVariant = 'primary' | 'default' | 'danger' | 'ghost'

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  loading?: boolean
  icon?: ReactNode
}

const BTN_VARIANT: Record<ButtonVariant, string> = {
  primary:
    'bg-brand text-white hover:bg-brand-hover active:bg-brand-dim shadow-lg shadow-brand/25 disabled:bg-brand/40',
  default:
    'bg-surface-card text-ink-primary border border-stroke hover:bg-surface-hover hover:border-[#35415f] active:bg-surface-active disabled:opacity-40',
  danger: 'bg-bad text-white hover:bg-[#f87171] active:bg-[#dc2626] shadow-lg shadow-bad/20 disabled:opacity-40',
  ghost:
    'text-ink-secondary hover:bg-surface-hover hover:text-ink-primary active:bg-surface-active disabled:opacity-40'
}

export function Button({ variant = 'default', loading, icon, children, className = '', disabled, ...rest }: ButtonProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={`inline-flex cursor-pointer items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-150 active:scale-[0.98] disabled:cursor-not-allowed ${BTN_VARIANT[variant]} ${className}`}
    >
      {loading ? <Loader2 size={15} className="animate-spin" /> : icon}
      {children}
    </button>
  )
}

// ---------- Input ----------

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string
  hint?: string
}

export function Input({ label, hint, className = '', id, ...rest }: InputProps) {
  const inputId = id ?? label
  return (
    <label htmlFor={inputId} className="block">
      {label && <span className="mb-1.5 block text-[13px] font-medium text-ink-secondary">{label}</span>}
      <input
        id={inputId}
        {...rest}
        className={`w-full rounded-lg border border-stroke bg-surface-card px-3 py-2 text-sm text-ink-primary placeholder:text-ink-faint transition-colors duration-150 hover:border-[#35415f] focus:border-brand focus:outline-none ${className}`}
      />
      {hint && <span className="mt-1.5 block text-xs text-ink-faint">{hint}</span>}
    </label>
  )
}

// ---------- Modal ----------

interface ModalProps {
  open: boolean
  title: string
  onClose?: () => void
  children: ReactNode
  footer?: ReactNode
  width?: number
}

export function Modal({ open, title, onClose, children, footer, width = 440 }: ModalProps) {
  const ref = useRef<HTMLDivElement>(null)
  // onClose 存 ref:调用方(如 SettingsPage closeForm)每次渲染产生新函数,
  // 若进入 effect 依赖,受控输入每敲一个字符 effect 重跑,初始聚焦会反复抢走输入焦点。
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const prevOpenRef = useRef(false)
  const previouslyFocusedRef = useRef<HTMLElement | null>(null)

  // 键盘监听(Esc 关闭 + Tab 焦点陷阱):仅随 open 挂载/卸载。
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && onCloseRef.current) onCloseRef.current()
      // 简易焦点陷阱:Tab 在弹窗内循环,不漏到背后页面
      if (e.key === 'Tab') {
        const container = ref.current
        if (!container) return
        const focusables = container.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        )
        if (focusables.length === 0) return
        const first = focusables[0]
        const last = focusables[focusables.length - 1]
        const active = document.activeElement
        if (e.shiftKey) {
          if (active === first || !container.contains(active)) {
            e.preventDefault()
            last.focus()
          }
        } else if (active === last || !container.contains(active)) {
          e.preventDefault()
          first.focus()
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // 初始聚焦 + 关闭还原:只在 open 的 false→true 边沿执行一次。
  // 不在 cleanup 里 cancel rAF:StrictMode 开发态重放 effect 时 prevOpenRef 已置 true,
  // 取消后无人再聚焦;改为 rAF 回调内检查目标仍挂载,天然防 open→close 竞态。
  useEffect(() => {
    const wasOpen = prevOpenRef.current
    prevOpenRef.current = open
    if (open && !wasOpen) {
      previouslyFocusedRef.current =
        document.activeElement instanceof HTMLElement ? document.activeElement : null
      requestAnimationFrame(() => {
        const target = pickInitialFocusTarget(ref.current)
        if (target?.isConnected) target.focus()
      })
    } else if (!open && wasOpen) {
      previouslyFocusedRef.current?.focus?.()
      previouslyFocusedRef.current = null
    }
  }, [open])

  // 卸载兜底:组件在 open 状态下被移除时也把焦点还给触发元素。
  useEffect(
    () => () => {
      previouslyFocusedRef.current?.focus?.()
    },
    []
  )

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 animate-fade-in"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && onClose) onClose()
      }}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        style={{ width }}
        className="animate-slide-up rounded-2xl border border-stroke bg-surface-card shadow-pop outline-none"
      >
        <div className="flex items-center justify-between border-b border-stroke-subtle px-5 py-4">
          <h2 className="text-[15px] font-semibold text-ink-primary">{title}</h2>
          {onClose && (
            <button
              onClick={onClose}
              aria-label="关闭"
              className="cursor-pointer rounded-md p-1 text-ink-muted transition-colors hover:bg-surface-hover hover:text-ink-primary"
            >
              <X size={16} />
            </button>
          )}
        </div>
        <div className="px-5 py-4">{children}</div>
        {footer && (
          <div className="flex justify-end gap-2 border-t border-stroke-subtle px-5 py-4">
            {footer}
          </div>
        )}
      </div>
    </div>
  )
}

// ---------- Toast(由 Toaster 集中渲染) ----------

export interface ToastItem {
  id: number
  kind: string
  message: string
}

const TOAST_ICON = {
  success: { icon: CheckCircle2, color: 'text-good' },
  warning: { icon: AlertTriangle, color: 'text-warn' },
  error: { icon: XCircle, color: 'text-bad' },
  info: { icon: AlertTriangle, color: 'text-brand' }
} as const

export function Toaster({ toasts, onDismiss }: { toasts: ToastItem[]; onDismiss: (id: number) => void }) {
  // 同屏最多 3 条:超出部分不渲染,防止错误风暴刷屏
  const visible = toasts.slice(-3)
  return (
    <div className="pointer-events-none fixed bottom-5 right-5 z-[100] flex flex-col gap-2" role="status" aria-live="polite">
      {visible.map((t) => {
        const view = TOAST_ICON[t.kind as keyof typeof TOAST_ICON] ?? TOAST_ICON.info
        const Icon = view.icon
        return (
          <div
            key={t.id}
            className="pointer-events-auto flex max-w-sm items-start gap-2.5 rounded-xl border border-stroke bg-surface-card px-4 py-3 shadow-pop animate-slide-up"
          >
            <Icon size={16} className={`mt-0.5 shrink-0 ${view.color}`} />
            <span className="text-[13px] leading-5 text-ink-primary">{t.message}</span>
            <button
              onClick={() => onDismiss(t.id)}
              aria-label="关闭提示"
              className="ml-1 cursor-pointer rounded p-0.5 text-ink-faint hover:text-ink-secondary"
            >
              <X size={13} />
            </button>
          </div>
        )
      })}
    </div>
  )
}

// ---------- 空态 ----------

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 py-16 text-center">
      <div className="mb-1 flex h-12 w-12 items-center justify-center rounded-2xl bg-surface-hover">
        <Inbox size={22} className="text-ink-faint" />
      </div>
      <div className="text-sm font-medium text-ink-secondary">{title}</div>
      {hint && <div className="max-w-xs text-xs leading-5 text-ink-faint">{hint}</div>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  )
}

// ---------- 状态徽标 ----------

const BADGE_STYLE = {
  idle: 'bg-surface-hover text-ink-muted',
  recording: 'bg-brand/15 text-brand',
  ended: 'bg-good/10 text-good',
  warn: 'bg-warn/10 text-warn',
  bad: 'bg-bad/10 text-bad'
} as const

export function Badge({
  tone = 'idle',
  children,
  dot = false,
  pulse = false
}: {
  tone?: keyof typeof BADGE_STYLE
  children: ReactNode
  dot?: boolean
  pulse?: boolean
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${BADGE_STYLE[tone]}`}
    >
      {dot && (
        <span className={`h-1.5 w-1.5 rounded-full bg-current ${pulse ? 'animate-pulse-dot' : ''}`} />
      )}
      {children}
    </span>
  )
}

// ---------- 加载 ----------

export function CenterSpin({ hint }: { hint?: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3">
      <Loader2 size={24} className="animate-spin text-brand" />
      {hint && <span className="text-xs text-ink-faint">{hint}</span>}
    </div>
  )
}
