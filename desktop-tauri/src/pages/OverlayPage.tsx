/**
 * 悬浮提词窗页面（第二个 webview，路由 `#/overlay`）。
 *
 * 三件事和主窗口不同：
 * 1. 自己订阅 `engine:event`。这个 webview 有独立 JS 上下文，`AppLayout` 里的订阅
 *    和 Zustand store 都在另一份实例里，拿不到。
 * 2. 自己给 body 挂 `.overlay-root`，把三层背景清成透明，只留页面内部的实色卡片。
 * 3. 顶栏是 `data-tauri-drag-region`（窗口 `decorations: false`，没有标题栏可拖）。
 *
 * 收起态（`overlay.collapsed`）渲染的是完全不同的一棵树：一条 18px 高的横细条，
 * 停在屏幕顶部居中（窗口已被 Rust 缩成这个尺寸）。不是给正常布局加 `hidden`，
 * 因为正常布局在那个尺寸下只会挤成一团乱码。
 *
 * 配色只有黑白灰（用户拍板）：文字走 white/透明度 灰阶，背景是纯中性黑
 * rgba(17,17,17,·)。状态之间用亮度区分（状态点、角标），不用彩色。
 */
import { useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react'
import {
  ArrowDownToLine,
  Camera,
  ChevronsDown,
  ChevronsDownUp,
  Eraser,
  Eye,
  GripHorizontal,
  Loader2,
  MousePointer2,
  Pin,
  PinOff,
  RefreshCw,
  SendHorizontal,
  Settings,
  ShieldCheck,
  ShieldOff,
  TriangleAlert,
  X
} from 'lucide-react'
import { api } from '../api/bridge'
import Markdown from '../components/Markdown'
import { buildAnswerFeed, pickDisplayVersion } from '../shared/answer-threads'
import { errorMessage } from '../shared/errors'
import { applyOverlayEvent, initialOverlayFeed } from '../shared/overlay-feed'
import { useOverlayControl } from '../shared/overlay-control'
import type { ConnectionPhase } from '../shared/types'

const PHASE_LABEL: Record<ConnectionPhase, string> = {
  idle: '未连接',
  connecting: '连接中',
  authenticating: '认证中',
  synchronizing: '同步中',
  ready: '已连接',
  reconnecting: '重连中',
  closed: '已断开'
}

// 黑白灰配色：状态只用亮度区分——ready 最亮（纯白），过渡态中灰，
// 断开/空闲最暗。不用彩色（用户拍板：悬浮窗只要黑白灰）。
const PHASE_DOT: Record<ConnectionPhase, string> = {
  idle: 'bg-white/40',
  connecting: 'bg-white/60',
  authenticating: 'bg-white/60',
  synchronizing: 'bg-white/60',
  ready: 'bg-white',
  reconnecting: 'bg-white/60',
  closed: 'bg-white/25'
}

/**
 * 自动滚动的判定余量（px）。
 *
 * 48 而不是 0：流式输出时 `scrollHeight` 每个 token 都在变，严格判等的话
 * 用户什么都没做也会被判成"手动滚上去了"，自动滚动直接失效。
 */
const TAIL_SLACK = 48

/** 工具条按钮。悬浮窗空间紧，只留图标 + title，尺寸仍守住 28px 可点区域。 */
function ToolButton({
  label,
  active,
  disabled,
  onClick,
  children
}: {
  label: string
  active?: boolean
  disabled?: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
      className={`flex h-7 w-7 cursor-pointer items-center justify-center rounded-md transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
        active
          ? 'bg-white/20 text-white'
          : 'text-white/55 hover:bg-white/10 hover:text-white'
      }`}
    >
      {children}
    </button>
  )
}

/*
 * 当前会话标识：悬浮窗必须一眼能看出自己跟的是哪场面试（后续要按会话把
 * 手机扫码接进来）。平时只显示前 8 位短 id，完整 id 在 tooltip 里，点击复制
 * （复制模式沿用 Markdown 代码块的防御式写法：非安全上下文 reject 就不装成功）。
 */
function SessionChip({ sessionId }: { sessionId: string }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    void navigator.clipboard
      ?.writeText(sessionId)
      .then(() => {
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1500)
      })
      .catch(() => undefined)
  }
  return (
    <button
      type="button"
      onClick={copy}
      title={`当前面试会话：${sessionId}（点击复制）`}
      aria-label="复制当前会话 ID"
      className="shrink-0 cursor-pointer rounded bg-white/10 px-1.5 py-px font-mono text-[10px] text-white/60 transition-colors hover:bg-white/20 hover:text-white"
    >
      {copied ? '已复制' : sessionId.slice(0, 8)}
    </button>
  )
}

export default function OverlayPage() {
  const [feed, dispatch] = useReducer(applyOverlayEvent, initialOverlayFeed)
  const { state: overlay, actions } = useOverlayControl()
  const bodyRef = useRef<HTMLDivElement>(null)
  const followTailRef = useRef(true)
  // 快速提问输入框：自动增高用（见下方 effect）。
  const questionInputRef = useRef<HTMLTextAreaElement>(null)
  const [atTail, setAtTail] = useState(true)
  const [showSettings, setShowSettings] = useState(false)
  const [solving, setSolving] = useState(false)
  const [solveHint, setSolveHint] = useState<{ kind: 'info' | 'warn'; text: string } | null>(null)
  // 底部快速提问输入框的内容与发送中状态。
  const [question, setQuestion] = useState('')
  const [sending, setSending] = useState(false)
  // Ctrl+Alt+Z 开启/暂停录制的执行中状态；同一时刻只允许一次。
  const [togglingRecording, setTogglingRecording] = useState(false)
  /*
   * 「最新」按钮的竞态修复：平滑回底（scrollToTail 的 behavior:'smooth'）动画
   * 期间每个中间帧都"不在底部"，handleScroll 照常判定会把刚隐藏的按钮又闪
   * 出来（用户实测的"先消失→一闪→再消失"）。动画期间抑制判定，落到底部
   * 即解除；定时器是安全阀——动画因故没走到尾时也不许把按钮永久藏住。
   */
  const jumpingToTailRef = useRef(false)
  const jumpTimeoutRef = useRef<number | null>(null)
  // 重新生成按钮冷却（按线程 key）。悬浮窗没有 toast，冷却就是唯一反馈。
  const [coolingKeys, setCoolingKeys] = useState<Set<string>>(new Set())
  const cooldownTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set())
  // toggleRecording 每次渲染都重建（闭包里是最新 feed）；订阅只做一次，经 ref 转发。
  const toggleRecordingRef = useRef<() => void>(() => {})

  useEffect(() => {
    const timers = cooldownTimersRef.current
    return () => {
      for (const t of timers) clearTimeout(t)
      timers.clear()
      if (jumpTimeoutRef.current !== null) window.clearTimeout(jumpTimeoutRef.current)
    }
  }, [])

  // 提示自动消失：悬浮窗没有 Toaster，提示直接长在工具条下面，留久了挡答案。
  useEffect(() => {
    if (!solveHint) return
    const timer = window.setTimeout(() => setSolveHint(null), 4000)
    return () => window.clearTimeout(timer)
  }, [solveHint])

  /*
   * 快速提问 textarea 自动增高：高度永远跟着内容走（先归 auto 再量
   * scrollHeight），不写死；封顶约 6 行（max-h-28），超出内部滚动。
   * 发送清空后 question 变 ''，同一个 effect 把高度收回一行。
   */
  useEffect(() => {
    const el = questionInputRef.current
    if (!el) return
    el.style.height = 'auto'
    const grown = el.scrollHeight
    el.style.height = `${grown}px`
    // Windows 缩放（125%/150%）下整数 scrollHeight 会比小数布局高度少零点
    // 几像素，overflow-y-auto 在单行时就画出滚动条。封顶（max-h-28=112px）
    // 之前直接把溢出藏掉——高度就是内容高度不可能真溢出；到顶后才恢复
    // 滚动，让第 7 行起内部滚动。
    el.style.overflowY = grown >= 112 ? 'auto' : 'hidden'
  }, [question])

  // 透明窗口需要 body/#root/html 三层都透明，靠这个 class 触发 global.css 里的规则。
  useEffect(() => {
    document.body.classList.add('overlay-root')
    return () => document.body.classList.remove('overlay-root')
  }, [])

  // Ctrl+Alt+Z 全局热键：Rust 只广播事件（见 run_overlay_shortcut 第 7 项），
  // 开启/暂停两个方向的动作都在悬浮窗里执行（见下方 toggleRecording）。
  useEffect(() => {
    let disposed = false
    let unlisten: (() => void) | undefined
    void api.overlay
      .onToggleRecordingHotkey(() => {
        toggleRecordingRef.current()
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

  useEffect(() => {
    let disposed = false
    let unlisten: (() => void) | undefined
    void api.events
      .on((event) => {
        if (!disposed) dispatch(event)
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

  /*
   * 播种：悬浮窗是懒创建的，打开之前发生的引擎事件（连接建立、开始录制、
   * 采集开关…）永远收不到——这正是"明明在录制却说未在录制中"的根因。
   * 挂载时向 Rust 拉一次运行时快照把状态补齐，之后的增量照常走上面的订阅。
   * 事件订阅先建、快照后到也安全：快照是 Rust 在响应时刻的最新值，只会比
   * 刚错过的事件更新不会更旧。
   */
  useEffect(() => {
    let disposed = false
    void api.live
      .runtimeState()
      .then((snapshot) => {
        if (!disposed) dispatch({ kind: 'overlay:seed', snapshot })
      })
      .catch(() => undefined)
    return () => {
      disposed = true
    }
  }, [])

  /*
   * 按住 Ctrl 时的滚轮：穿透 + Ctrl 临时交互下用户要能滚答案区，但
   * Chromium/WebView2 把 Ctrl+滚轮当页面缩放手势吃掉，滚动容器收不到普通
   * 滚动——这正是"只能手动拖滚动条"的根因。在 document 上以捕获 + 非
   * passive 拦下：preventDefault 阻止缩放，把增量应用到光标下最近的可滚动
   * 容器（多行 textarea / 答案区 / partial 区），自己滚。非 Ctrl 的滚轮
   * 不碰，走浏览器原生滚动。
   */
  useEffect(() => {
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return
      event.preventDefault()
      let node = event.target instanceof Element ? event.target : null
      while (node) {
        if (node.scrollHeight > node.clientHeight) {
          const { overflowY } = window.getComputedStyle(node)
          if (overflowY === 'auto' || overflowY === 'scroll') {
            const amount =
              event.deltaMode === WheelEvent.DOM_DELTA_LINE
                ? event.deltaY * 16
                : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
                  ? event.deltaY * node.clientHeight
                  : event.deltaY
            node.scrollTop += amount
            return
          }
        }
        node = node.parentElement
      }
    }
    document.addEventListener('wheel', onWheel, { passive: false, capture: true })
    return () => document.removeEventListener('wheel', onWheel, { capture: true })
  }, [])

  const model = useMemo(
    () => buildAnswerFeed(Object.values(feed.streaming), feed.answers),
    [feed.streaming, feed.answers]
  )

  useLayoutEffect(() => {
    const node = bodyRef.current
    if (node && followTailRef.current) node.scrollTop = node.scrollHeight
    // 依赖里必须有 overlay.collapsed：收起/展开会卸载重挂滚动容器，重挂后
    // scrollTop 归零，必须重新贴底，否则展开后聊天记录显示在顶部。
    // feed.pending 也要贴底：刚发出的"正在思考"卡得出现在视野里。
  }, [model.threads, feed.partial, feed.pending, overlay.collapsed])

  const handleScroll = () => {
    const node = bodyRef.current
    if (!node) return
    const tail = node.scrollHeight - node.scrollTop - node.clientHeight < TAIL_SLACK
    if (jumpingToTailRef.current) {
      // 平滑回底动画进行中：中间帧距离底部必然超过 TAIL_SLACK，照常判定会
      // 把刚隐藏的「最新」按钮又闪出来。落到底部（tail 为真）才解除抑制；
      // 中间帧一律忽略。安全阀见 scrollToTail。
      if (tail) {
        jumpingToTailRef.current = false
        if (jumpTimeoutRef.current !== null) {
          window.clearTimeout(jumpTimeoutRef.current)
          jumpTimeoutRef.current = null
        }
        followTailRef.current = true
        setAtTail(true)
      }
      return
    }
    followTailRef.current = tail
    // `atTail` 只驱动"回到底部"按钮的显隐，滚动判断仍走 ref：
    // setState 会重渲染，流式输出时每帧滚动都重渲染整棵树太贵。
    setAtTail((prev) => (prev === tail ? prev : tail))
  }

  const scrollToTail = () => {
    const node = bodyRef.current
    if (!node) return
    jumpingToTailRef.current = true
    if (jumpTimeoutRef.current !== null) window.clearTimeout(jumpTimeoutRef.current)
    // 安全阀：动画 600ms 内没走到尾（内容暴涨把目标顶远、用户中途反向滚）
    // 也要解除抑制，否则「最新」按钮会被永久藏住。
    jumpTimeoutRef.current = window.setTimeout(() => {
      jumpingToTailRef.current = false
      jumpTimeoutRef.current = null
    }, 600)
    node.scrollTo({ top: node.scrollHeight, behavior: 'smooth' })
    followTailRef.current = true
    setAtTail(true)
  }

  const clearAnswers = () => {
    dispatch({ kind: 'overlay:clear' })
    followTailRef.current = true
    setAtTail(true)
  }

  const phase = feed.phase
  const recording = feed.sessionStatus === 'recording'
  /*
   * 徽标语义（用户拍板：必须一眼分清"真的在录"与"没在录"）：
   * 会话状态 recording 只代表这场面试开始了，不代表本机在采——切收音模式
   * 要求会话保持 recording，主窗口也是分开显示这两件事的。
   * - 录制中：本机采集门开着（呼吸点在跳）
   * - 录制中 · 手机：收音走手机，本机不采
   * - 录制中 · 采集已停：会话还开着但本机门关了（主窗口同样有此提示）
   */
  const mobileCapture = recording && feed.radioMode === 'mobile'
  const capturing = recording && feed.captureOn
  const badgeText = recording
    ? mobileCapture
      ? '录制中 · 手机'
      : capturing
        ? '录制中'
        : '录制中 · 采集已停'
    : PHASE_LABEL[phase]

  // 截图解题。悬浮窗自己开着共享隐身（WDA_EXCLUDEFROMCAPTURE），所以抓到的帧里
  // 不会有它自己，不用先隐藏窗口再抓。
  //
  // 反馈只能就地显示：`app-toast` 那条链路挂在主窗口的 AppLayout 上，这个 webview
  // 是独立 JS 上下文，派发过去没人接。
  /*
   * 会话侧拦截的细分提示：门禁拦下时把真实原因说出来，不再一律
   * "未在录制中"。状态只有一个来源（后端会话状态，经每次连接的
   * sync_complete 和后续 session_state 事件同步进来），这里只是把它
   * 翻译成人话——看到提示就知道是哪种情况，不用猜：
   * - ended：这场真的结束了（结束面试只发生在主窗口）。
   * - idle：会话还没开始录制，Ctrl+Alt+Z 一键开启。
   * - ''（未知）：刚连上还在同步 / 引擎重连中，事件到达即恢复。
   */
  const sessionBlockHint = (action: string) => {
    if (feed.sessionStatus === 'ended') return `这场面试已结束，无法${action}`
    if (feed.sessionStatus === 'idle') return `还没开始录制，按 Ctrl+Alt+Z 开启后再${action}`
    return `会话状态同步中，稍候再${action}`
  }
  const solveScreenshot = async () => {
    if (solving) return
    // 与提问同一套门禁：只看连接与会话（暂停期间截图解题照常可用），
    // 提示语与真实拦因一致，每种状态一句话说清为什么。
    if (phase !== 'ready') {
      setSolveHint({ kind: 'warn', text: '连接未就绪，稍候再试' })
      return
    }
    if (!recording) {
      setSolveHint({ kind: 'warn', text: sessionBlockHint('解题') })
      return
    }
    setSolving(true)
    setSolveHint(null)
    try {
      const ok = await api.live.solveScreenshot()
      setSolveHint(
        ok
          ? { kind: 'info', text: '截图已发送，正在解题' }
          : { kind: 'warn', text: '截图发送失败，检查连接' }
      )
    } catch (err) {
      setSolveHint({ kind: 'warn', text: errorMessage(err) })
    } finally {
      setSolving(false)
    }
  }

  const answerCount = model.threads.length + model.historyAnswers.length

  /*
   * 底部快速提问：不想等面试官说、或想追问时，直接在这里把问题敲给 AI。
   * 走不带 thread_id 的手动提问路径（与主窗口手动提问同一条），答案独立
   * 成一张卡流回上方。穿透状态下也能用：按住 Ctrl 点进输入框后焦点还在，
   * 松开 Ctrl 键盘输入不受影响（穿透只拦鼠标）。
   */
  const submitQuestion = async () => {
    const text = question.trim()
    if (!text || sending) return
    // 门禁只看连接与会话，不看采集开关：暂停（Ctrl+Alt+Z）只停音频链路，
    // 手动提问不走音频，暂停期间照常可问。拦因按 sessionStatus 细分通报
    // （见 sessionBlockHint），别再拿"未在录制中"一句话盖住四种不同情况。
    if (phase !== 'ready') {
      setSolveHint({ kind: 'warn', text: '连接未就绪，稍候再试' })
      return
    }
    if (!recording) {
      setSolveHint({ kind: 'warn', text: sessionBlockHint('提问') })
      return
    }
    setSending(true)
    setSolveHint(null)
    try {
      const ok = await api.live.regenerate(text, false)
      if (!ok) throw new Error('问题发送失败，请确认连接状态')
      setQuestion('')
      // 发送成功立刻挂"正在思考"卡：有没有发出去、AI 开始答没有，一眼可见，
      // 不用猜也不用重发。answer_stream 首帧（started 空帧）到达即自动交棒。
      dispatch({ kind: 'overlay:asked', question: text })
      setSolveHint({ kind: 'info', text: '已发送，正在思考' })
    } catch (err) {
      setSolveHint({ kind: 'warn', text: errorMessage(err) })
    } finally {
      setSending(false)
    }
  }

  // 问题卡重新生成：带 thread_id，后端 revision+1 并流回同一张卡。
  // 生成中也可点；反馈就地显示，按钮进入冷却转圈。
  const handleRegenerateThread = async (key: string, question: string) => {
    if (coolingKeys.has(key)) return
    const timer = setTimeout(() => {
      cooldownTimersRef.current.delete(timer)
      setCoolingKeys((prev) => {
        const next = new Set(prev)
        next.delete(key)
        return next
      })
    }, 6500)
    cooldownTimersRef.current.add(timer)
    setCoolingKeys((prev) => new Set(prev).add(key))
    try {
      await api.live.regenerate(question, false, key)
    } catch {
      // 悬浮窗够不到主窗口的 toast；失败时卡片不动本身就是反馈。
    }
  }

  /*
   * Ctrl+Alt+Z：开启录制 / 暂停录制（用户拍板 2026-09-02，开关语义）。
   *
   * 语义红线：这个键**永远不结束会话**。"暂停"掐断的是音频链路
   * （系统声音 → ASR → LLM）：停采集、关上传门禁，没有新转写与新答案
   * 刷屏；会话保持 recording，随时恢复。结束面试只在主窗口。
   * 三个分支：
   * - idle：开启。start_session → 系统采集 → 上传门禁，与主窗口同序
   *   （采集起不来就不开门，门开了没声音等于白录）。
   * - recording 且本机在采：暂停（停采集 → 关门禁）。暂停期间手动提问
   *   不受影响——它不走音频链路。
   * - recording 但本机没采（= 已暂停）：恢复（起采集 → 开门禁）。
   * 手机收音模式的采集在手机上，本机无从暂停，给提示不代按。
   */
  const toggleRecording = async () => {
    if (togglingRecording) return
    if (phase !== 'ready') {
      setSolveHint({ kind: 'warn', text: '未连接实时面试，请先在主窗口打开面试' })
      return
    }
    if (feed.sessionStatus === 'ended') {
      setSolveHint({ kind: 'warn', text: '这场面试已结束' })
      return
    }
    if (feed.sessionStatus === 'recording') {
      if (feed.radioMode === 'mobile') {
        setSolveHint({ kind: 'warn', text: '手机收音模式，请在手机端暂停' })
        return
      }
      setTogglingRecording(true)
      setSolveHint(null)
      try {
        if (feed.captureOn) {
          await api.audio.stopSystem()
          await api.outbox.setCaptureActive(false)
          setSolveHint({ kind: 'info', text: '已暂停录制（面试仍在进行，可继续提问）' })
        } else {
          const started = await api.audio.startSystem()
          if (!started) throw new Error('无法开始系统声音采集')
          await api.outbox.setCaptureActive(true)
          setSolveHint({ kind: 'info', text: '已继续录制' })
        }
      } catch (err) {
        if (!feed.captureOn) await api.audio.stopSystem().catch(() => {})
        setSolveHint({ kind: 'warn', text: errorMessage(err) })
      } finally {
        setTogglingRecording(false)
      }
      return
    }
    if (feed.sessionStatus !== 'idle') {
      setSolveHint({ kind: 'warn', text: '会话状态同步中，请稍候再按' })
      return
    }
    setTogglingRecording(true)
    setSolveHint(null)
    try {
      const ok = await api.live.startSession(feed.radioMode || 'pc')
      if (!ok) throw new Error('主窗口未连接实时面试')
      const started = await api.audio.startSystem()
      if (!started) throw new Error('无法开始系统声音采集')
      await api.outbox.setCaptureActive(true)
      setSolveHint({ kind: 'info', text: '已开始录制' })
    } catch (err) {
      await api.audio.stopSystem().catch(() => {})
      setSolveHint({ kind: 'warn', text: errorMessage(err) })
    } finally {
      setTogglingRecording(false)
    }
  }

  useEffect(() => {
    toggleRecordingRef.current = toggleRecording
  })

  /*
   * 收起态：窗口已被 Rust 缩成 18px 高的横条（顶部居中），这里只画一条能点的细条。
   * 整条都是按钮而不是"细条 + 展开按钮"：18px 高放不下两个可点区，而且用户
   * 在这个尺寸下唯一想做的事就是展开。内容只有状态点 + 向下箭头——收起条的
   * 唯一职责是"还在这儿"，挤进文字只会把它变成第二块要读的内容。
   * 不加投影：透明窗口上 box-shadow 会在圆角外的窗口矩形四角留下灰色蒙层。
   */
  if (overlay.collapsed) {
    return (
      <button
        type="button"
        title="展开悬浮窗（Ctrl+Alt+E）"
        aria-label="展开悬浮窗"
        onClick={() => void actions.expand()}
        className="overlay-strip flex h-full w-full cursor-pointer flex-row items-center justify-center gap-1.5 rounded-md border border-white/10 px-2 text-white/55 transition-colors hover:text-white"
        style={{ backgroundColor: `rgba(17, 17, 17, ${overlay.opacity})` }}
      >
        <span
          className={`h-1.5 w-1.5 shrink-0 rounded-full ${PHASE_DOT[phase]} ${
            capturing ? 'animate-pulse-dot' : ''
          }`}
          aria-hidden
        />
        <ChevronsDown size={11} aria-hidden />
      </button>
    )
  }

  return (
    <div
      className="overlay-card overlay-panel flex h-full flex-col overflow-hidden rounded-xl border border-white/10 text-white"
      style={
        {
          backgroundColor: `rgba(17, 17, 17, ${overlay.opacity})`,
          // 滚动条颜色跟随不透明度（global.css 的 overlay-root 滚动条规则消费
          // 这两个变量）：底越不透明 thumb 越亮；半透明时压暗，不抢戏。
          '--overlay-thumb': `rgba(255, 255, 255, ${(0.16 + overlay.opacity * 0.32).toFixed(3)})`,
          '--overlay-thumb-hover': `rgba(255, 255, 255, ${(0.3 + overlay.opacity * 0.4).toFixed(3)})`
        } as React.CSSProperties
      }
    >
      {/*
       * 不透明度只落在背景层（rgba），文字和控件保持全亮：整卡套 opacity 会把
       * 文字一起调淡，25% 时等于把提词内容弄丢。薄纱底 + 全亮字才是想要的。
       * 不加投影：透明窗口上 box-shadow 画在窗口矩形的四角（圆角外），看起来
       * 就是一圈洗不掉的灰色蒙层。
       */}
      {/* 顶栏：左半是拖动区，右半是工具条（工具条不能落在 drag region 里，否则点不动） */}
      <header className="flex shrink-0 items-center gap-1 border-b border-white/10 px-2 py-1.5">
        <div
          data-tauri-drag-region
          className="flex min-w-0 flex-1 items-center gap-2 py-0.5 pl-1"
          title="拖动移动窗口"
        >
          <GripHorizontal size={13} className="shrink-0 text-white/40" aria-hidden />
          <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full ${PHASE_DOT[phase]} ${
              capturing ? 'animate-pulse-dot' : ''
            }`}
            aria-hidden
          />
          <span className="truncate text-[11px] text-white/60">{badgeText}</span>
          {/*
           * 模式标识。spec 点名要"明显的状态提示"，而且穿透态下点不动任何东西，
           * 用户必须能一眼看出是穿透了还是应用卡了。用文字而不是只换图标颜色。
           * 穿透 + 按住 Ctrl = Rust 轮询给的临时交互（松手自动回穿透）。
           * 亮度即层级：临时交互最亮（当前可点），穿透最暗。
           */}
          <span
            className={`shrink-0 rounded px-1.5 py-px text-[10px] font-medium ${
              overlay.passthrough
                ? overlay.ctrlInteractive
                  ? 'bg-white/20 text-white'
                  : 'bg-white/10 text-white/60'
                : 'bg-white/10 text-white/75'
            }`}
          >
            {overlay.passthrough
              ? overlay.ctrlInteractive
                ? '临时交互'
                : '鼠标穿透'
              : '交互模式'}
          </span>
        </div>

        {feed.sessionId ? <SessionChip sessionId={feed.sessionId} /> : null}

        <div className="flex shrink-0 items-center gap-0.5">
          <ToolButton
            label={
              recording
                ? '截图解题（Ctrl+Alt+Q）：抓当前屏幕交给 AI 解题'
                : '截图解题需要先在主窗口开始录制'
            }
            disabled={solving || !recording || phase !== 'ready'}
            onClick={() => void solveScreenshot()}
          >
            {solving ? <Loader2 size={13} className="animate-spin" /> : <Camera size={13} />}
          </ToolButton>
          <ToolButton
            label={overlay.passthrough ? '关闭鼠标穿透' : '开启鼠标穿透（点击穿到底层窗口）'}
            active={overlay.passthrough}
            onClick={() => void actions.setPassthrough(!overlay.passthrough)}
          >
            <MousePointer2 size={13} />
          </ToolButton>
          <ToolButton
            label={overlay.alwaysOnTop ? '取消始终置顶' : '始终置顶'}
            active={overlay.alwaysOnTop}
            onClick={() => void actions.setAlwaysOnTop(!overlay.alwaysOnTop)}
          >
            {overlay.alwaysOnTop ? <Pin size={13} /> : <PinOff size={13} />}
          </ToolButton>
          <ToolButton
            label="收起成顶部细条（Ctrl+Alt+E）"
            onClick={() => void actions.collapse()}
          >
            <ChevronsDownUp size={13} />
          </ToolButton>
          <ToolButton
            label="清空当前回答（历史记录仍在主窗口）"
            disabled={answerCount === 0 && !feed.partial}
            onClick={clearAnswers}
          >
            <Eraser size={13} />
          </ToolButton>
          <ToolButton
            label="设置"
            active={showSettings}
            onClick={() => setShowSettings((prev) => !prev)}
          >
            <Settings size={13} />
          </ToolButton>
          <ToolButton label="隐藏悬浮窗（Ctrl+Alt+O 再唤出）" onClick={() => void actions.hide()}>
            <X size={13} />
          </ToolButton>
        </div>
      </header>

      {/*
       * 设置面板。折进来而不是常驻工具条：透明度、共享隐身这些是"摆好一次"
       * 的开关，摆好之后天天占着顶栏只会挤掉答案空间。
       */}
      {showSettings && (
        <div className="overlay-panel shrink-0 space-y-2 border-b border-white/10 bg-white/[0.03] px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <span className="shrink-0 text-[10px] text-white/55">背景不透明度</span>
            <div className="flex min-w-0 flex-1 items-center gap-2 pl-3">
              <input
                type="range"
                min={25}
                max={100}
                step={5}
                value={Math.round(overlay.opacity * 100)}
                aria-label="背景不透明度"
                onChange={(e) => void actions.setOpacity(Number(e.target.value) / 100)}
                className="h-1 min-w-0 flex-1 cursor-pointer accent-white"
              />
              <span className="tnum w-9 shrink-0 text-right text-[11px] text-white/75">
                {Math.round(overlay.opacity * 100)}%
              </span>
            </div>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-[10px] text-white/55">
              共享隐身
              <span className="ml-1 text-white/40">只挡录屏，挡不住手机拍屏</span>
            </span>
            <ToolButton
              label={overlay.contentProtected ? '关闭共享隐身' : '开启共享隐身'}
              active={overlay.contentProtected}
              onClick={() => void actions.setContentProtected(!overlay.contentProtected)}
            >
              {overlay.contentProtected ? <ShieldCheck size={13} /> : <ShieldOff size={13} />}
            </ToolButton>
          </div>
        </div>
      )}

      {/* 就地反馈（解题/提问）：主窗口的 toast 到不了这个 webview。灰阶里 warn 比 info 亮一档+粗边框。 */}
      {solveHint && (
        <div
          role="status"
          className={`shrink-0 border-b px-3 py-1.5 text-[10px] ${
            solveHint.kind === 'info'
              ? 'border-white/15 bg-white/[0.06] text-white/75'
              : 'border-white/30 bg-white/[0.08] text-white'
          }`}
        >
          {solveHint.text}
        </div>
      )}

      {/* 当前问题：面试官还在说的累计 partial，答案在下面同步刷新 */}
      {feed.partial && (
        <div className="shrink-0 border-b border-white/10 bg-white/[0.03] px-3 py-2">
          <div className="mb-0.5 flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-white/40">
            <Loader2 size={10} className="animate-spin" />
            正在提问
          </div>
          <div className="max-h-16 overflow-y-auto text-[12px] leading-5 text-white/75">
            {feed.partial.text}
          </div>
        </div>
      )}

      {/* 答案区。relative 是为了让"回到底部"浮标定位在这一块的右下角。 */}
      <div className="relative min-h-0 flex-1">
      <div ref={bodyRef} onScroll={handleScroll} className="h-full overflow-y-auto px-3 py-2.5">
        {answerCount === 0 && feed.pending.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-4 text-center">
            <Eye size={18} className="text-white/40" aria-hidden />
            <div className="text-[12px] text-white/55">等待面试官提问</div>
            <div className="text-[10px] leading-4 text-white/40">
              主窗口开始录制后，答案会实时出现在这里
            </div>
          </div>
        ) : (
          <div className="space-y-2.5">
            {model.threads.map((thread) => {
              const display = pickDisplayVersion(thread.versions)
              return (
                <article
                  key={thread.key}
                  className="overlay-answer-enter rounded-lg border border-white/15 bg-white/[0.05] p-2.5"
                  aria-live="polite"
                >
                  {/*
                   * 标题与重新生成按钮同行（space-between + gap）：大宽度悬浮窗下
                   * 按钮挤在卡底浪费一整行，靠右和标题对齐更紧凑。标题 flex-1
                   * 可换行，按钮 shrink-0 不被压缩。
                   */}
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1 select-text text-[12px] font-semibold leading-5 text-white">
                      {thread.question}
                    </div>
                    {/*
                     * 常驻重新生成：不管答案出来没有、是否还在生成，都能点——
                     * 生成卡住时这是唯一的自救手段。后端会取消该线程在途任务。
                     */}
                    <button
                      type="button"
                      title="重新生成这张卡的答案（生成中也可点，会取消当前生成重开）"
                      disabled={coolingKeys.has(thread.key)}
                      onClick={() => void handleRegenerateThread(thread.key, thread.question)}
                      className="mt-0.5 flex h-6 shrink-0 cursor-pointer items-center gap-1 rounded-md px-1.5 text-[10px] text-white/55 transition-colors hover:bg-white/10 hover:text-white disabled:cursor-not-allowed disabled:opacity-45"
                    >
                      {coolingKeys.has(thread.key) ? (
                        <Loader2 size={10} className="animate-spin" />
                      ) : (
                        <RefreshCw size={10} />
                      )}
                      重新生成
                    </button>
                  </div>
                  {/*
                   * catch-up swap：一张卡同一时刻只展示一段——未被取代的段里答案
                   * 最长的那个（新版流式追平旧版长度即自然接管，永不闪断）。
                   * 标题已经是累计问题，段内不再重复"问题："一行。
                   */}
                  {display && display.answer ? (
                    <Markdown source={display.answer} variant="answer" />
                  ) : (
                    <div className="flex items-center gap-1.5 text-[10px] text-white/45">
                      {display?.failed ? (
                        <TriangleAlert size={10} className="text-white/60" />
                      ) : (
                        <Loader2 size={10} className="animate-spin" />
                      )}
                      {display?.failed ? '生成失败，可点重新生成' : '正在生成…'}
                    </div>
                  )}
                </article>
              )
            })}
            {model.historyAnswers.map((answer) => (
              <article
                key={answer.id}
                className="overlay-answer-enter rounded-lg border border-white/10 bg-white/[0.04] p-2.5"
              >
                <div className="mb-1.5 select-text text-[12px] font-semibold leading-5 text-white">
                  {answer.question}
                </div>
                <Markdown source={answer.answer} variant="answer" />
              </article>
            ))}
            {/*
             * 刚发出的手动提问（pending）：发送成功即出现在这里，answer_stream
             * 首帧到达自动消失、由真卡接棒。用户由此确认"发出去了、AI 在想"，
             * 不用反复重发。
             */}
            {feed.pending.map((p) => (
              <article
                key={p.id}
                className="overlay-answer-enter rounded-lg border border-white/15 bg-white/[0.05] p-2.5"
                aria-live="polite"
              >
                <div className="select-text text-[12px] font-semibold leading-5 text-white">{p.question}</div>
                <div className="flex items-center gap-1.5 text-[10px] text-white/45">
                  <Loader2 size={10} className="animate-spin" />
                  已发送 · 正在思考
                </div>
              </article>
            ))}
          </div>
        )}
      </div>

      {/*
       * 一键回到底部。只在用户手动滚上去之后出现：常驻的话会一直挡住答案右下角，
       * 而自动滚动本来就在跟着底部，那个按钮 90% 的时间没有意义。
       */}
      {!atTail && answerCount > 0 && (
        <button
          type="button"
          onClick={scrollToTail}
          title="回到最新答案"
          aria-label="回到最新答案"
          className="overlay-panel absolute bottom-2.5 right-3 flex h-7 items-center gap-1 rounded-full border border-white/30 bg-[#111111] px-2.5 text-[10px] text-white transition-colors hover:bg-white/15"
        >
          <ArrowDownToLine size={11} />
          最新
        </button>
      )}
      </div>

      {/*
       * 底部快速提问（用户拍板：原热键说明区换成输入框）：不想等面试官说、
       * 或想追问时直接敲问题。Enter 发送、Shift+Enter 换行；textarea 高度跟
       * 内容走（自动增高，封顶约 6 行）；发送按钮是右下角的图标——单行时
       * 恰好垂直居中在右侧，多行时贴住右下角。走手动提问路径，答案独立成卡
       * 流回上方。未录制时按钮禁用——截图解题按钮同一条规则。
       *
       * 与答案区的分隔靠"面"不靠"线"（用户拍板 2026-09-02 的四层方案，
       * 全黑白灰）：footer 整块抬一档操作面（white/[0.04]），输入容器再亮
       * 一档（white/[0.08] + 边框 white/20 + rounded-lg），答案卡最暗
       * （white/[0.05]）——三档亮度一眼分清"内容层 / 操作层"。聚焦反馈走
       * 灰阶提亮（边框 white/35 + 背景 white/[0.10]），不画彩色描边。
       * 快捷键说明从 placeholder 挪到输入框下方微字，placeholder 只留动作
       * 导向的「向 AI 提问…」。悬浮窗例外：聚焦仍不画 outline（global.css
       * 里 overlay-root 的表单控件例外），边框提亮已足够表达焦点。
       */}
      <footer className="shrink-0 border-t border-white/10 bg-white/[0.04] px-3 pb-2 pt-3">
        <form
          onSubmit={(event) => {
            event.preventDefault()
            void submitQuestion()
          }}
        >
          <div className="relative">
            <textarea
              ref={questionInputRef}
              value={question}
              onChange={(event) => setQuestion(event.target.value.slice(0, 2000))}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  // 直调 submitQuestion 而非 requestSubmit：下方发送按钮在
                  // 未连接/未录制时是 disabled，requestSubmit 对被禁用的默认
                  // 提交按钮是静默无操作——门禁提示永远亮不出来，用户只会
                  // 看到"按了 Enter 毫无反应"。直调保证四种拦因都能通报。
                  void submitQuestion()
                }
              }}
              rows={1}
              placeholder="向 AI 提问…"
              aria-label="向 AI 快速提问"
              className="max-h-28 w-full resize-none overflow-y-auto rounded-lg border border-white/20 bg-white/[0.08] py-1.5 pl-2.5 pr-8 text-[11px] leading-4 text-white outline-none transition-colors placeholder:text-white/35 focus:border-white/35 focus:bg-white/[0.10]"
            />
            <button
              type="submit"
              disabled={sending || !question.trim() || phase !== 'ready' || !recording}
              title={phase === 'ready' && recording ? '发送问题（Enter）' : '开始录制后才能提问'}
              aria-label="发送问题"
              className="absolute bottom-0.5 right-1 flex h-6 w-6 cursor-pointer items-center justify-center rounded text-white/85 transition-colors hover:bg-white/15 hover:text-white disabled:cursor-not-allowed disabled:opacity-45"
            >
              {sending ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <SendHorizontal size={13} />
              )}
            </button>
          </div>
          {/* 快捷键微字提示：从 placeholder 挪到输入框下方，placeholder 只留「向 AI 提问…」 */}
          <div className="mt-1.5 px-0.5 text-[10px] leading-3 text-white/35">
            Enter 发送 · Shift+Enter 换行
          </div>
        </form>
      </footer>
    </div>
  )
}
