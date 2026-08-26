/**
 * 错误消息归一化:后端/命令层可能抛 Error、字符串或普通对象。
 * Tauri invoke 拒绝时通常是字符串;直接 (err as Error).message 会得到 undefined。
 */
export function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  if (typeof error === 'string') return error
  if (
    error !== null &&
    typeof error === 'object' &&
    typeof (error as { message?: unknown }).message === 'string' &&
    (error as { message: string }).message
  ) {
    return (error as { message: string }).message
  }
  return String(error)
}

/** toast 快捷方式:走 App 层全局 app-toast 事件(与页面级 toast 同一出口)。 */
export function emitToast(kind: 'success' | 'error' | 'warning' | 'info', message: string): void {
  window.dispatchEvent(new CustomEvent('app-toast', { detail: { kind, message } }))
}
