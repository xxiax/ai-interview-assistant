import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  CircleStop,
  Headphones,
  Laptop,
  Layers,
  Loader2,
  ScanText,
  SendHorizontal,
  Target,
  Volume2,
  VolumeX,
  Smartphone
} from 'lucide-react'
import { api } from '../api/bridge'
import { useLiveStore } from '../stores/live'
import type { RadioMode, Session } from '../shared/types'
import { errorMessage } from '../shared/errors'
import { useOverlayControl } from '../shared/overlay-control'
import { groupFinalTranscripts } from '../shared/transcript-display'
import { buildAnswerFeed } from '../shared/answer-threads'
import { Badge, Button, CenterSpin, Modal } from '../components/ui'
import OverlayPanel from '../components/OverlayPanel'
import SessionContextModal from '../components/SessionContextModal'
import TranscriptFeed from './feeds/TranscriptFeed'
import AnswerFeed from './feeds/AnswerFeed'

// React StrictMode 会执行一次 mount → cleanup → mount。Tauri 只有一个全局 Live
// 引擎，因此所有连接生命周期必须串行，避免旧 cleanup 晚于新 connect 执行。
let liveLifecycleQueue: Promise<void> = Promise.resolve()

function enqueueLiveLifecycle<T>(operation: () => Promise<T>): Promise<T> {
  const result = liveLifecycleQueue.then(
    () => operation(),
    () => operation()
  )
  liveLifecycleQueue = result.then(
    () => undefined,
    () => undefined
  )
  return result
}

function showToast(kind: 'success' | 'error' | 'warning' | 'info', message: string) {
  window.dispatchEvent(new CustomEvent('app-toast', { detail: { kind, message } }))
}

// ---------- 收音模式分段控件 ----------

const MODES: { value: RadioMode; label: string; icon: typeof Laptop }[] = [
  { value: 'pc', label: '电脑收音', icon: Laptop },
  // mobile 源分片只能由移动客户端上传;PC 端切过去只会让自己的分片被拒
  { value: 'mobile', label: '手机收音', icon: Smartphone },
  { value: 'both', label: '双端收音', icon: Headphones }
]

function ModeSwitch({
  value,
  disabled,
  onChange
}: {
  value: RadioMode
  disabled: boolean
  onChange: (m: RadioMode) => void
}) {
  return (
    <div
      role="radiogroup"
      aria-label="收音模式"
      className="flex items-center gap-0.5 rounded-lg border border-stroke bg-surface-card p-0.5"
    >
      {MODES.map((m) => {
        const active = value === m.value
        return (
          <button
            key={m.value}
            role="radio"
            aria-checked={active}
            disabled={disabled}
            onClick={() => onChange(m.value)}
            title={m.value === 'mobile' ? '等待手机端接入收音(本机系统声音暂停上传)' : undefined}
            className={`flex cursor-pointer items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-40 ${
              active ? 'bg-brand text-white shadow' : 'text-ink-secondary hover:bg-surface-hover'
            }`}
          >
            <m.icon size={13} />
            {m.label}
          </button>
        )
      })}
    </div>
  )
}

// ---------- 顶部状态栏 ----------

const PHASE_TEXT: Record<string, { tone: 'idle' | 'recording' | 'ended' | 'warn' | 'bad'; text: string }> = {
  idle: { tone: 'idle', text: '未连接' },
  connecting: { tone: 'warn', text: '连接中…' },
  authenticating: { tone: 'warn', text: '认证中…' },
  synchronizing: { tone: 'warn', text: '同步中…' },
  ready: { tone: 'ended', text: '已连接' },
  reconnecting: { tone: 'warn', text: '重连中…' },
  closed: { tone: 'bad', text: '已断开' }
}

function TopBar(props: {
  title: string
  phase: string
  synced: boolean
  recording: boolean
  radioMode: RadioMode
  queued: number
  contextReady: boolean
  overlayVisible: boolean
  onModeChange: (m: RadioMode) => void
  onOpenContext: () => void
  onOpenOverlay: () => void
  onEnd: () => void
  ending: boolean
  modeChanging: boolean
}) {
  const view = PHASE_TEXT[props.phase] ?? PHASE_TEXT.idle
  return (
    <header className="flex items-center justify-between border-b border-stroke bg-surface-raised px-5 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <h1 className="truncate text-[15px] font-semibold text-ink-primary">{props.title}</h1>
        <Badge tone={view.tone} dot pulse={props.phase === 'reconnecting'}>
          {view.text}
        </Badge>
        {props.phase === 'ready' && !props.synced && <Badge tone="warn">事件同步中</Badge>}
        {props.queued > 0 && (
          <Badge tone="idle">
            队列 {props.queued}
          </Badge>
        )}
      </div>
      <div className="flex items-center gap-3">
        <Button
          variant="default"
          icon={<Target size={14} />}
          onClick={props.onOpenContext}
          title={
            props.contextReady
              ? '已填岗位 JD / 简历，AI 会按这个岗位答题'
              : '填岗位 JD 和简历，让答案贴合这个岗位而不是通用答案'
          }
        >
          答题背景
          {props.contextReady && (
            <span
              aria-label="已配置"
              className="h-1.5 w-1.5 rounded-full bg-good shadow-[0_0_6px_rgba(34,197,94,0.6)]"
            />
          )}
        </Button>
        <Button
          variant="default"
          icon={<Layers size={14} />}
          onClick={props.onOpenOverlay}
          title="悬浮提词窗：盖在会议窗口之上显示答案，可对屏幕共享隐身"
        >
          悬浮窗
          {props.overlayVisible && (
            <span
              aria-label="已显示"
              className="h-1.5 w-1.5 rounded-full bg-good shadow-[0_0_6px_rgba(34,197,94,0.6)]"
            />
          )}
        </Button>
        <ModeSwitch
          value={props.radioMode}
          disabled={!props.recording || props.modeChanging}
          onChange={props.onModeChange}
        />
        <Button
          variant="danger"
          icon={<CircleStop size={15} />}
          loading={props.ending}
          disabled={!props.recording}
          onClick={props.onEnd}
        >
          结束面试
        </Button>
      </div>
    </header>
  )
}

// ---------- 答案栏 ----------
//
// H2:answer_stream 的 token 帧只改 streamingAnswers/pendingQuestions,
// 这两片订阅下沉到本组件——token 帧只重渲染答案栏,LivePage 顶栏/转写栏/
// 底栏全部跳过。卡内再靠 React.memo(AnswerFeed)与 Markdown 解析缓存
// 把重渲染继续压缩到正在流式的那一张卡。
function AnswerPanel() {
  const streamingAnswers = useLiveStore((s) => s.streamingAnswers)
  const answers = useLiveStore((s) => s.answers)
  const pending = useLiveStore((s) => s.pendingQuestions)
  // 一个问题一张卡：后端对累计 partial 的有效 revision 并发请求 LLM；
  // 展示层保留各版状态，但同一时刻只渲染 catch-up swap 选中的一版。
  // 已落库答案挂到对应卡片上，不再单独渲染重复卡。
  const { historyAnswers, threads } = useMemo(
    () => buildAnswerFeed(Object.values(streamingAnswers), answers),
    [streamingAnswers, answers]
  )
  return (
    <section className="flex min-w-0 flex-1 flex-col border-l border-stroke" aria-label="AI 答案">
      <div className="flex items-center gap-2 border-b border-brand/20 bg-brand/[0.045] px-6 py-2.5">
        <span className="h-1.5 w-1.5 rounded-full bg-good shadow-[0_0_8px_rgba(34,197,94,0.55)]" />
        <span className="text-[13px] font-semibold text-ink-primary">AI 回答建议</span>
        <span className="tnum ml-auto text-[11px] text-ink-faint">
          {historyAnswers.length + threads.length + pending.length} 条
        </span>
      </div>
      <div className="min-h-0 flex-1 bg-[radial-gradient(circle_at_top_right,rgba(79,124,255,0.055),transparent_38%)]">
        <AnswerFeed answers={historyAnswers} threads={threads} pending={pending} />
      </div>
    </section>
  )
}

// ---------- 主页面 ----------

export default function LivePage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  // H2:按片订阅(全部返回原始值或 store 内稳定引用),token 帧改不到的
  // 片不再触发本组件重渲染;streamingAnswers/pendingQuestions 的订阅在
  // AnswerPanel 里,LivePage 本体对 token 帧零重渲染。
  const phase = useLiveStore((s) => s.phase)
  const sessionStatus = useLiveStore((s) => s.sessionStatus)
  const radioMode = useLiveStore((s) => s.radioMode)
  const synced = useLiveStore((s) => s.synced)
  const queued = useLiveStore((s) => s.outbox?.queued ?? 0)
  const note = useLiveStore((s) => s.note)
  const captureStateOn = useLiveStore((s) => s.captureOn)
  const seqWatermarkReady = useLiveStore((s) => s.seqWatermarkReady)
  const audioFault = useLiveStore((s) => s.audioFault)
  const systemAudioFault = useLiveStore((s) => s.systemAudioFault)
  const transcripts = useLiveStore((s) => s.transcripts)
  const partialTranscript = useLiveStore((s) => s.partialTranscript)
  const transcriptDisplayCount = useMemo(
    () => groupFinalTranscripts(transcripts).length,
    [transcripts]
  )
  const [sessionTitle, setSessionTitle] = useState('')
  // 会话快照：标题之外还带 job_description / resume，供答题背景弹窗回填。
  const [session, setSession] = useState<Session | null>(null)
  const [contextOpen, setContextOpen] = useState(false)
  const [overlayOpen, setOverlayOpen] = useState(false)
  // 只用它的 state.visible 点亮 TopBar 上那颗绿点；真正的动作都在 OverlayPanel 里。
  // 两处各订阅一次 overlay:state，热键改了状态两边都会跟上。
  const { state: overlayState } = useOverlayControl()
  const [ending, setEnding] = useState(false)
  const [startOpen, setStartOpen] = useState(false)
  const [startMode, setStartMode] = useState<RadioMode>('pc')
  const [systemAudioOn, setSystemAudioOn] = useState(false)
  const [captureStarting, setCaptureStarting] = useState(false)
  const [modeChanging, setModeChanging] = useState(false)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [manualQuestion, setManualQuestion] = useState('')
  const [manualSending, setManualSending] = useState(false)
  const [solving, setSolving] = useState(false)
  const startedRef = useRef(false)
  const captureEpochRef = useRef(0)
  const captureStartingRef = useRef(false)
  const systemAudioOnRef = useRef(false)
  const modeChangingRef = useRef(false)

  const stopCapture = useCallback(
    async (reason: 'capture_stopped' | 'source_disabled' = 'capture_stopped') => {
      // 快照本次停止对应的采集代;新采集开始会递增 epoch,届时本函数的收尾
      // 不得再执行(否则迟到一步的 setCaptureActive(false) 会关掉新 gate)。
      const stopEpoch = captureEpochRef.current
      // 系统声音由 Rust 线程组帧，stopSystem 完成后再关闭上传门禁，
      // 避免停止边界的完整系统音频分片落入取消水位之后。
      captureStartingRef.current = false
      systemAudioOnRef.current = false
      setCaptureStarting(false)
      setSystemAudioOn(false)
      captureEpochRef.current = Math.max(captureEpochRef.current, stopEpoch + 1)
      const results: PromiseSettledResult<unknown>[] = []
      results.push(...(await Promise.allSettled([api.audio.stopSystem()])))
      if (captureEpochRef.current !== stopEpoch + 1) return // 新采集已接管,跳过收尾
      results.push(...(await Promise.allSettled([api.outbox.setCaptureActive(false, reason)])))
      const rejected = results.find((result) => result.status === 'rejected')
      if (rejected?.status === 'rejected') {
        showToast('warning', `停止后台音频任务失败：${String(rejected.reason)}`)
      }
    },
    []
  )

  // 连接 + 订阅(事件由 App 层统一订阅进 store)
  useEffect(() => {
    if (!sessionId) return
    let disposed = false
    useLiveStore.getState().reset(sessionId)
    setSessionTitle('')
    setSession(null)
    setContextOpen(false)
    setStartOpen(false)
    setStartMode('pc')
    startedRef.current = false
    // 历史回填:reset 清空了 store,但此前的转写/答案都在后端。与连接并行拉取;
    // mergeHistory 是并集语义,实时事件先到也不会被晚到的历史覆盖。
    setHistoryLoading(true)
    void Promise.all([
      api.history.transcripts(sessionId),
      api.history.answers(sessionId)
    ])
      .then(([transcripts, answers]) => {
        if (disposed) return
        useLiveStore.getState().mergeHistory(sessionId, transcripts, answers)
      })
      .catch((err) => {
        // 回填失败不阻塞实时流,只提示
        if (!disposed) showToast('warning', `同步历史记录失败：${errorMessage(err)}`)
      })
      .finally(() => {
        if (!disposed) setHistoryLoading(false)
      })

    void api.sessions
      .get(sessionId)
      .then((s) => {
        if (disposed) return
        setSessionTitle(s.title)
        setSession(s)
        if (s.status === 'ended') {
          navigate(`/session/${sessionId}`, { replace: true })
        } else if (s.status === 'idle') {
          setStartMode(s.radio_mode)
          setStartOpen(true)
        } else if (s.status === 'recording') {
          startedRef.current = true
          // REST 兜底:即使连接事件早于全局监听注册，模式 UI 仍采用服务端权威值。
          useLiveStore.setState({ sessionStatus: 'recording', radioMode: s.radio_mode })
        }
      })
      .catch((err) => {
        if (!disposed) showToast('error', `读取面试状态失败：${errorMessage(err)}`)
      })

    void enqueueLiveLifecycle(async () => {
      if (disposed) return
      const connected = await api.live.connect(sessionId)
      if (!connected) throw new Error('Live 引擎未能启动')
      if (disposed) return
      // SeqWatermark 事件理论上会到达；snapshot 是监听注册竞态的兜底。Rust
      // 才是序号权威，因此即使这里只读到本地水位也不会产生碰撞。
      const stats = await api.outbox.snapshot()
      if (disposed || useLiveStore.getState().sessionId !== sessionId) return
       const nextChunkSeq = Math.max(0, stats.next_chunk_seq ?? 0)
       useLiveStore.setState({ seqWatermark: nextChunkSeq, seqWatermarkReady: true })
    }).catch((err) => {
      if (!disposed) showToast('error', `连接面试失败：${errorMessage(err)}`)
    })

    return () => {
      disposed = true
      // 先停止系统音频，再断开引擎，确保停止边界的原生分片先落盘。
      const stopping = stopCapture()
      void enqueueLiveLifecycle(async () => {
        await stopping
        await api.live.disconnect()
      }).catch(() => {})
    }
  }, [navigate, sessionId, stopCapture])

  const handleStart = async () => {
    try {
      const ok = await api.live.startSession(startMode)
      if (!ok) throw new Error('发送开始请求失败，请确认连接状态')
      setStartOpen(false)
      startedRef.current = true
      // 会话状态广播可能早于事件监听就绪而丢失；直接置位，后续事件幂等覆盖。
      useLiveStore.setState({ sessionStatus: 'recording', radioMode: startMode })
    } catch (err) {
      showToast('error', errorMessage(err))
    }
  }

  const handleModeChange = async (mode: RadioMode) => {
    if (modeChangingRef.current || mode === useLiveStore.getState().radioMode) return
    if (phase !== 'ready') {
      showToast('warning', '连接未就绪，稍等片刻再切换收音模式')
      return
    }
    const previousMode = useLiveStore.getState().radioMode
    modeChangingRef.current = true
    setModeChanging(true)
    try {
      const ok = await api.live.setRadioMode(mode)
      if (!ok) throw new Error('切换请求发送失败，请检查连接')
      // invoke 成功表示 Rust 已按顺序接收命令；立即反馈 UI，服务端的
      // session_state 随后会再次以权威状态覆盖。
      useLiveStore.setState({ radioMode: mode })
      if (mode === 'mobile') await stopCapture('source_disabled')
      const label = MODES.find((m) => m.value === mode)?.label ?? mode
      showToast('success', `已切换为${label}`)
    } catch (err) {
      useLiveStore.setState({ radioMode: previousMode })
      showToast('error', errorMessage(err))
    } finally {
      modeChangingRef.current = false
      setModeChanging(false)
    }
  }

  const handleEnd = useCallback(async () => {
    if (!sessionId) return
    await stopCapture()
    setEnding(true)
    try {
      const ok = await api.live.endSession()
      if (!ok) throw new Error('结束请求发送失败，请返回列表后重试')
    } catch (err) {
      showToast('error', errorMessage(err))
    } finally {
      setEnding(false)
    }
  }, [sessionId, stopCapture])

  const handleManualQuestion = async () => {
    const question = manualQuestion.trim()
    if (!question || manualSending) return
    if (phase !== 'ready' || sessionStatus !== 'recording') {
      showToast('warning', '面试连接未就绪，暂时无法向 AI 提问')
      return
    }
    setManualSending(true)
    try {
      const ok = await api.live.regenerate(question, false)
      if (!ok) throw new Error('问题发送失败，请确认连接和答案队列状态')
      setManualQuestion('')
      // 发送成功立刻挂"正在思考"卡：有没有发出去、AI 开始答没有一眼可见，
      // 不用盯着按钮转圈猜。answer_stream 首帧（started 空帧）到达自动交棒。
      useLiveStore.getState().addPendingQuestion(question)
    } catch (err) {
      showToast('error', errorMessage(err))
    } finally {
      setManualSending(false)
    }
  }

  /**
   * 笔试辅助：抓当前屏幕交给后端多模态解题。
   *
   * 输入框里的文字当作备注一起送过去（"只解第二题"这类），送出后清空，
   * 因为它已经消费掉了。答案按普通 answer 事件回到右侧列表。
   */
  const handleSolveScreenshot = async () => {
    if (solving) return
    if (phase !== 'ready' || sessionStatus !== 'recording') {
      showToast('warning', '面试连接未就绪，暂时无法解题')
      return
    }
    setSolving(true)
    try {
      const ok = await api.live.solveScreenshot(manualQuestion.trim() || undefined)
      if (!ok) throw new Error('截图发送失败，请确认连接和答案队列状态')
      setManualQuestion('')
      showToast('info', '截图已发送，正在解题')
    } catch (err) {
      showToast('error', errorMessage(err))
    } finally {
      setSolving(false)
    }
  }

  const toggleCapture = async () => {    if (!sessionId || captureStartingRef.current) return
    // 悬浮窗 Ctrl+Alt+Z 开的采集：本页 systemAudioOn 没翻，但上传门禁开着
    // （store 的 captureOn 跟着 captureState 事件走）。这里统一导向停止，
    // 不能误判成"没在录"再开一次。
    if (systemAudioOn || useLiveStore.getState().captureOn) {
      await stopCapture()
      return
    }
    const current = useLiveStore.getState()
    if (current.radioMode === 'mobile') {
      showToast('warning', '手机收音模式下，本机系统声音不会上传')
      return
    }
    if (
      current.sessionStatus !== 'recording' ||
      current.phase !== 'ready' ||
      !current.seqWatermarkReady
    ) {
      showToast('warning', '连接或分片序号尚未初始化完成，请稍后再试')
      return
    }

    captureStartingRef.current = true
    setCaptureStarting(true)
    const captureEpoch = captureEpochRef.current + 1
    captureEpochRef.current = captureEpoch
    const systemResult = (await Promise.allSettled([api.audio.startSystem()]))[0]
    try {
      if (
        captureEpochRef.current !== captureEpoch ||
        useLiveStore.getState().radioMode === 'mobile'
      ) {
        await api.audio.stopSystem().catch(() => {})
        return
      }

      const systemStarted = systemResult.status === 'fulfilled' && systemResult.value
      systemAudioOnRef.current = systemStarted
      setSystemAudioOn(systemStarted)
      if (!systemStarted) {
        const reason = systemResult.status === 'rejected' ? String(systemResult.reason) : '当前设备不支持系统音频回环'
        throw new Error(`无法开始系统声音采集：${reason}`)
      }

      await api.outbox.setCaptureActive(true)
    } catch (err) {
      if (captureEpochRef.current === captureEpoch) {
        showToast('error', errorMessage(err))
        await stopCapture()
      } else {
        await api.audio.stopSystem().catch(() => {})
      }
    } finally {
      if (captureEpochRef.current === captureEpoch) {
        captureStartingRef.current = false
        setCaptureStarting(false)
      }
    }
  }

  // 模式也可能由重放事件或另一端修改；任何 mobile 权威状态都立即停本机采集。
  useEffect(() => {
    if (radioMode === 'mobile' && systemAudioOnRef.current) {
      void stopCapture('source_disabled')
    }
  }, [radioMode, stopCapture])

  // 会话状态与连接失步时停止采集,避免分片被逐个标错(session_not_recording 级联)
  useEffect(() => {
    if (
      systemAudioOnRef.current &&
      (phase === 'closed' || phase === 'reconnecting')
    ) {
      void stopCapture()
      showToast('warning', '连接中断，已暂停采集；恢复连接后请重新开始')
    }
  }, [phase, stopCapture])

  // 音频处理上游不可用时立即熔断本次采集。仅停止录音器不够；stopCapture 还会
  // 取消 Rust outbox 与服务端已排队分片，避免旧任务继续报错。
  useEffect(() => {
    if (audioFault && systemAudioOnRef.current) {
      void stopCapture()
    }
  }, [audioFault, stopCapture])

  useEffect(() => {
    if (!systemAudioFault || !systemAudioOnRef.current) return
    systemAudioOnRef.current = false
    setSystemAudioOn(false)
    void stopCapture()
  }, [systemAudioFault, stopCapture])

  // 会话 ended 后自动跳详情
  useEffect(() => {
    if (sessionStatus === 'ended' && sessionId && startedRef.current) {
      const t = setTimeout(() => navigate(`/session/${sessionId}`, { replace: true }), 800)
      return () => clearTimeout(t)
    }
  }, [sessionStatus, sessionId, navigate])

  const connecting = phase === 'idle' || phase === 'connecting'
  // 按钮状态跟着事件走(captureState),不能只看本地 systemAudioOn:悬浮窗
  // Ctrl+Alt+Z 开的采集也要让这个按钮如实变成"停止系统采集"。
  const captureOn = systemAudioOn || captureStateOn
  const contextReady = !!(session?.job_description?.trim() || session?.resume?.trim())

  return (
    <div className="flex h-full flex-col">
      <TopBar
        title={sessionTitle || '面试进行中'}
        phase={phase}
        synced={synced}
        recording={sessionStatus === 'recording'}
        radioMode={radioMode}
        queued={queued}
        contextReady={contextReady}
        overlayVisible={overlayState.visible}
        onModeChange={handleModeChange}
        onOpenContext={() => setContextOpen(true)}
        onOpenOverlay={() => setOverlayOpen(true)}
        onEnd={() => void handleEnd()}
        ending={ending}
        modeChanging={modeChanging}
      />

      {/* 断线提示条 */}
      {phase === 'closed' && note && (
        <div className="border-b border-bad/25 bg-bad/10 px-5 py-2 text-xs text-bad">
          {note}
        </div>
      )}

      {/* 双栏:转写 | 答案 */}
      <div className="flex min-h-0 flex-1">
        <section
          className="flex w-[32%] min-w-[280px] max-w-[420px] flex-col bg-surface/55"
          aria-label="实时转写"
        >
          <div className="flex items-center gap-2 border-b border-stroke-subtle bg-surface/80 px-4 py-2.5">
            <span className="h-1.5 w-1.5 rounded-full bg-ink-muted" />
            <span className="text-[12px] font-medium text-ink-muted">实时转写</span>
            {historyLoading && (
              <span className="flex items-center gap-1 text-[11px] text-ink-faint">
                <Loader2 size={11} className="animate-spin" />
                同步历史记录…
              </span>
            )}
            <span className="tnum ml-auto text-[11px] text-ink-faint">
              {transcriptDisplayCount} 条
            </span>
          </div>
          <div className="min-h-0 flex-1">
            {connecting ? (
              <CenterSpin hint="连接服务…" />
            ) : (
              <TranscriptFeed
                transcripts={transcripts}
                partialTranscript={partialTranscript}
              />
            )}
          </div>
          <form
            onSubmit={(event) => {
              event.preventDefault()
              void handleManualQuestion()
            }}
            className="border-t border-stroke-subtle bg-surface/70 p-3"
          >
            <div className="flex items-center gap-2 rounded-xl border border-stroke-subtle bg-surface-raised/90 px-2.5 py-1.5 shadow-[0_8px_24px_rgba(0,0,0,0.12)]">
              <textarea
                value={manualQuestion}
                onChange={(event) => setManualQuestion(event.target.value.slice(0, 2000))}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault()
                    event.currentTarget.form?.requestSubmit()
                  }
                }}
                rows={1}
                placeholder="输入问题，按 Enter 发送给 AI"
                aria-label="向 AI 手动提问"
                className="agent-prompt-input h-9 max-h-24 min-h-9 flex-1 resize-none bg-transparent py-2 text-left text-[13px] leading-5 text-ink-primary outline-none placeholder:text-ink-faint "
              />
              <button
                type="button"
                onClick={() => void handleSolveScreenshot()}
                disabled={solving || phase !== 'ready' || sessionStatus !== 'recording'}
                aria-label="截图解题"
                title="截图解题（Ctrl+Alt+Q）；输入框内容会作为备注一起发送"
                className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-lg border border-stroke-subtle bg-surface text-ink-secondary shadow-sm transition-colors hover:bg-surface-hover hover:text-ink-primary disabled:cursor-not-allowed disabled:text-ink-faint disabled:opacity-100"
              >
                {solving ? (
                  <Loader2 size={15} className="animate-spin" />
                ) : (
                  <ScanText size={15} />
                )}
              </button>
              <button
                type="submit"
                disabled={
                  !manualQuestion.trim() ||
                  manualSending ||
                  phase !== 'ready' ||
                  sessionStatus !== 'recording'
                }
                aria-label="发送问题"
                title="Enter 发送，Shift+Enter 换行"
                className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-lg bg-brand text-white shadow-sm transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:bg-surface-hover disabled:text-ink-faint disabled:opacity-100"
              >
                {manualSending ? (
                  <Loader2 size={15} className="animate-spin" />
                ) : (
                  <SendHorizontal size={15} />
                )}
              </button>
            </div>
          </form>
        </section>

        {/* 答案栏自订阅 streamingAnswers/answers/pendingQuestions(H2 见 AnswerPanel) */}
        <AnswerPanel />
      </div>

      {/* 底部:电脑端系统声音采集控制 */}
      <footer className="flex items-center justify-between border-t border-stroke bg-surface-raised px-5 py-3">
        <div className="flex items-center gap-3">
          <button
            onClick={() => void toggleCapture()}
            disabled={
              sessionStatus !== 'recording' ||
              phase !== 'ready' ||
              !seqWatermarkReady ||
              radioMode === 'mobile' ||
              captureStarting
            }
            aria-pressed={captureOn}
            className={`flex cursor-pointer items-center gap-2.5 rounded-xl px-5 py-2.5 text-sm font-medium transition-all duration-150 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 ${
              captureOn
                ? 'bg-bad text-white shadow-lg shadow-bad/25 hover:bg-[#f87171]'
                : 'bg-brand text-white shadow-lg shadow-brand/25 hover:bg-brand-hover'
            }`}
          >
            {captureStarting ? (
              <Loader2 size={16} className="animate-spin" />
            ) : captureOn ? (
              <VolumeX size={16} />
            ) : (
              <Volume2 size={16} />
            )}
            {captureStarting ? '启动中…' : captureOn ? '停止系统采集' : '开始系统采集'}
          </button>
          {!captureOn && sessionStatus === 'recording' && (
            <span className="text-xs text-ink-faint">
              {radioMode === 'mobile'
                ? '手机收音模式下，本机采集保持关闭'
                : seqWatermarkReady
                   ? '开始后仅采集电脑播放声音（含腾讯会议对方声音）'
                  : '正在初始化分片序号…'}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-[11px] text-ink-faint">
          {captureOn && (
            <span className="flex items-center gap-1.5 text-good">
              <span className="h-1.5 w-1.5 animate-pulse-dot rounded-full bg-good" />
               采集中 · 系统声音
            </span>
          )}
        </div>
      </footer>

      {/* 开始弹窗 */}
      <Modal
        open={startOpen}
        title="开始面试"
        footer={
          <>
            <Button variant="ghost" onClick={() => navigate('/')}>
              返回列表
            </Button>
            <Button
              variant="primary"
              disabled={phase !== 'ready'}
              onClick={() => void handleStart()}
            >
              开始
            </Button>
          </>
        }
      >
        <p className="mb-3 text-[13px] text-ink-secondary">选择初始收音模式(进行中可切换)</p>
        <div className="grid grid-cols-3 gap-2" role="radiogroup" aria-label="初始收音模式">
          {MODES.map((m) => (
            <button
              key={m.value}
              role="radio"
              aria-checked={startMode === m.value}
              onClick={() => setStartMode(m.value)}
              className={`flex cursor-pointer flex-col items-center gap-2 rounded-xl border px-3 py-4 transition-all duration-150 ${
                startMode === m.value
                  ? 'border-brand bg-brand/10 text-brand'
                  : 'border-stroke bg-surface-card text-ink-secondary hover:bg-surface-hover'
              }`}
            >
              <m.icon size={20} />
              <span className="text-xs font-medium">{m.label}</span>
            </button>
          ))}
        </div>
        {phase !== 'ready' && (
          <div className="mt-3 flex items-center gap-2 text-xs text-ink-faint">
            <Loader2 size={12} className="animate-spin" />
            等待连接就绪…
          </div>
        )}
        {!contextReady && (
          <div className="mt-4 rounded-lg border border-warn/25 bg-warn/[0.07] px-3 py-2.5 text-xs leading-5 text-ink-secondary">
            还没填岗位 JD 和简历，AI 只能给通用答案。
            <button
              onClick={() => setContextOpen(true)}
              className="ml-1 cursor-pointer font-medium text-brand underline-offset-2 hover:underline"
            >
              现在补上
            </button>
            （面试中途也能改）
          </div>
        )}
      </Modal>

      {sessionId && (
        <SessionContextModal
          open={contextOpen}
          sessionId={sessionId}
          session={session}
          onClose={() => setContextOpen(false)}
          onSaved={(next) => setSession(next)}
        />
      )}

      <OverlayPanel open={overlayOpen} onClose={() => setOverlayOpen(false)} />
    </div>
  )
}
