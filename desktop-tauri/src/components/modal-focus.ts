/**
 * Modal 初始聚焦目标选择(纯函数,便于行为测试)。
 * 优先表单控件/链接,避免聚焦到 header 里 DOM 顺序第一的关闭按钮;
 * 无控件时退回容器内第一个可用按钮,最后容器自身(dialog 本身 tabIndex=-1 可聚焦)。
 */
export function pickInitialFocusTarget(container: HTMLElement | null): HTMLElement | null {
  if (!container) return null
  const control = container.querySelector<HTMLElement>('input, select, textarea, [href]')
  if (control) return control
  const button = container.querySelector<HTMLElement>('button:not([disabled])')
  return button ?? container
}
