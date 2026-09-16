/**
 * 主窗口里的悬浮提词窗控制面板。
 *
 * 面试时主窗口通常被会议软件盖住，用户看不到它；所以这个面板只负责"面试前配置好"：
 * 开窗、透明度、共享隐身、鼠标穿透，然后把快捷键摊开给用户记。
 * 面试中真正的操作入口是全局热键和悬浮窗自己的工具条。
 */
import { ChevronsLeftRight, MonitorOff, ShieldCheck, TriangleAlert } from 'lucide-react'
import { Button, Modal } from './ui'
import { OVERLAY_HOTKEYS, useOverlayControl } from '../shared/overlay-control'

/** 一行开关。左侧标题 + 说明，右侧一个 role=switch 的按钮。 */
function ToggleRow({
  title,
  hint,
  checked,
  disabled,
  onChange
}: {
  title: string
  hint: string
  checked: boolean
  disabled?: boolean
  onChange: (next: boolean) => void
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-2.5">
      <div className="min-w-0">
        <div className="text-[13px] font-medium text-ink-primary">{title}</div>
        <div className="mt-0.5 text-[11px] leading-4 text-ink-faint">{hint}</div>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={title}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`relative mt-0.5 h-5 w-9 shrink-0 cursor-pointer rounded-full transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-50 ${
          checked ? 'bg-brand' : 'bg-surface-active'
        }`}
      >
        {/* 圆点必须锚定 left-0.5 再平移：不写 left 时绝对定位回落到静态位置，
            圆点会被按钮的 text-align 挤到中间，开/关两个状态的位置都是错的。
            按钮 36px、圆点 16px：左 2px + 平移 16px，两侧各留 2px 边距。 */}
        <span
          className={`absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white transition-transform duration-150 ${
            checked ? 'translate-x-4' : 'translate-x-0'
          }`}
        />
      </button>
    </div>
  )
}

export default function OverlayPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { state, busy, actions } = useOverlayControl()
  const opacityPercent = Math.round(state.opacity * 100)

  return (
    <Modal open={open} title="悬浮提词窗" onClose={onClose} width={480}>
      <div className="space-y-4">
        <div className="flex items-center justify-between gap-3 rounded-lg border border-stroke bg-surface-card px-3.5 py-3">
          <div className="min-w-0">
            <div className="text-[13px] font-medium text-ink-primary">
              {state.visible ? '悬浮窗已显示' : '悬浮窗未显示'}
            </div>
            <div className="mt-0.5 text-[11px] text-ink-faint">
              盖在会议窗口之上，实时显示答案。Ctrl+Alt+O 随时显隐
            </div>
            {/*
              焦点实时播报：悬浮窗从不主动抢焦，但用户点了它之后键盘输入会进
              悬浮窗——面试时这是要立刻发现的事，所以跟着 overlay:state 事件
              实时显示，而不是只在打开面板时读一次。
            */}
            {state.visible && (
              <div className="mt-0.5 text-[11px] text-ink-faint">
                {state.focused
                  ? '键盘焦点正在悬浮窗上，现在打字会进悬浮窗'
                  : '键盘焦点不在悬浮窗上，打字不受影响'}
              </div>
            )}
          </div>
          <Button
            variant={state.visible ? 'default' : 'primary'}
            disabled={busy}
            onClick={() => void actions.toggle()}
          >
            {state.visible ? '隐藏' : '显示'}
          </Button>
        </div>

        <div className="divide-y divide-stroke-subtle">
          <ToggleRow
            title="共享隐身"
            hint="对系统截屏 / 录屏 / 会议共享隐藏这个窗口"
            checked={state.contentProtected}
            disabled={busy}
            onChange={(next) => void actions.setContentProtected(next)}
          />
          <ToggleRow
            title="鼠标穿透"
            hint="开启后点击穿到底层会议窗口，悬浮窗只看不点"
            checked={state.passthrough}
            disabled={busy}
            onChange={(next) => void actions.setPassthrough(next)}
          />
          <ToggleRow
            title="始终置顶"
            hint="关掉后会被会议窗口盖住"
            checked={state.alwaysOnTop}
            disabled={busy}
            onChange={(next) => void actions.setAlwaysOnTop(next)}
          />
        </div>

        {/*
          收起不是开关而是一次动作：收起后悬浮窗只剩屏幕顶部中间一条 18px 细条，
          用户会直接在细条上点回来，不会跑回主窗口找这个开关。所以这里给一个
          按钮而不是 ToggleRow。
        */}
        <div className="flex items-center justify-between gap-3 rounded-lg border border-stroke bg-surface-card px-3.5 py-3">
          <div className="min-w-0">
            <div className="text-[13px] font-medium text-ink-primary">
              {state.collapsed ? '当前已收起成细条' : '收起成细条'}
            </div>
            <div className="mt-0.5 text-[11px] text-ink-faint">
              任意位置都能收起：窗口变成屏幕顶部居中的一条细条，Ctrl+Alt+E 展开
            </div>
          </div>
          <Button
            disabled={busy}
            onClick={() => void (state.collapsed ? actions.expand() : actions.collapse())}
          >
            <ChevronsLeftRight size={13} className="mr-1" />
            {state.collapsed ? '展开' : '收起'}
          </Button>
        </div>

        {/*
          滑条而不是档位按钮：25%-100% 每 5% 一格，和热键（每次 5%）同一套步进，
          鼠标拖出来的值和热键按出来的值不会互相打乱。
          滑条只调背景层：整卡套 opacity 会把文字一起调淡，低档位时提词内容
          直接看不清；文字必须保持全亮。
        */}
        <div>
          <div className="mb-1.5 flex items-center justify-between">
            <span className="text-[13px] font-medium text-ink-primary">背景不透明度</span>
            <span className="tnum text-[11px] text-ink-muted">{opacityPercent}%</span>
          </div>
          <input
            type="range"
            min={25}
            max={100}
            step={5}
            value={opacityPercent}
            disabled={busy}
            aria-label="背景不透明度"
            onChange={(e) => void actions.setOpacity(Number(e.target.value) / 100)}
            className="w-full cursor-pointer accent-brand disabled:cursor-not-allowed disabled:opacity-50"
          />
          <div className="mt-1.5 flex justify-between text-[11px] text-ink-faint">
            <span>25%</span>
            <span>Ctrl+Alt+= / Ctrl+Alt+- 每次调 5%</span>
            <span>100%</span>
          </div>
          <div className="mt-1 text-[11px] leading-4 text-ink-faint">
            只调背景浓度，文字始终全亮可读
          </div>
        </div>

        <div>
          <div className="mb-1.5 text-[13px] font-medium text-ink-primary">全局快捷键</div>
          <ul className="grid grid-cols-2 gap-x-4 gap-y-1.5">
            {OVERLAY_HOTKEYS.map((hotkey) => (
              <li key={hotkey.keys} className="flex items-center justify-between gap-2">
                <span className="truncate text-[11px] text-ink-muted">{hotkey.label}</span>
                <kbd className="tnum shrink-0 rounded border border-stroke bg-surface-card px-1.5 py-px text-[10px] text-ink-secondary">
                  {hotkey.keys}
                </kbd>
              </li>
            ))}
          </ul>
          <div className="mt-1.5 text-[11px] leading-4 text-ink-faint">
            系统级注册，焦点在会议软件里也生效。被其他程序占用时对应热键会失效，面板按钮仍可用
          </div>
        </div>

        {/*
          隐身边界必须写清楚。WDA_EXCLUDEFROMCAPTURE 只对 OS 采集 API 生效，
          对物理采集手段无效；把这句藏起来才是真的坑用户。
        */}
        <div className="rounded-lg border border-warn/30 bg-warn/[0.07] px-3.5 py-3">
          <div className="mb-1 flex items-center gap-1.5 text-[12px] font-medium text-warn">
            <TriangleAlert size={12} />
            隐身能力的边界
          </div>
          <ul className="space-y-1 text-[11px] leading-4 text-ink-secondary">
            <li className="flex gap-1.5">
              <ShieldCheck size={11} className="mt-0.5 shrink-0 text-good" aria-hidden />
              挡得住：会议软件的屏幕共享、录屏软件、系统截图（Windows 10 2004 及以上）
            </li>
            <li className="flex gap-1.5">
              <MonitorOff size={11} className="mt-0.5 shrink-0 text-bad" aria-hidden />
              挡不住：HDMI 采集卡、手机对着屏幕拍、有人站在你身后
            </li>
          </ul>
        </div>
      </div>
    </Modal>
  )
}
