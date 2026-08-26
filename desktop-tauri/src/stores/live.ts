/**
 * 实时会话 store:连接状态 + 事件 reducer(去重/追加/计数)。
 * 与 desktop/src/renderer/src/stores/live.ts 同构,适配 Tauri 事件。
 */
import { create } from 'zustand'
import type { Answer, AnswerSource, ConnectionPhase, EngineEvent, OutboxStats, PartialTranscript, RadioMode, SessionStatus, StreamingAnswer, Transcript } from '../shared/types'

export interface LiveState {
  sessionId: string | null
  phase: ConnectionPhase
  note?: string
  sessionStatus: SessionStatus | ''
  radioMode: RadioMode
  transcripts: Transcript[]
  partialTranscript: PartialTranscript | null
  answers: Answer[]
  streamingAnswers: Record<string, StreamingAnswer>
  lastEventId: number
  synced: boolean
  outbox: OutboxStats | null
  /** 序号水位(引擎权威值;LivePage 用它对齐 sequencer) */
  seqWatermark: number
  /** 引擎已推送过水位(采集前置条件:防止 watermark 未到就开麦从 0 撞号) */
  seqWatermarkReady: boolean
  /** 音频处理故障熔断信号；LivePage 收到后立即停止采集并取消积压。 */
  audioFault: { id: number; message: string } | null
  systemAudioFault: { id: number; message: string } | null
  /** 全局 toast 队列(由 App 层消费) */
  toasts: { id: number; kind: string; message: string }[]
  apply: (event: EngineEvent) => void
  /** App 层全局订阅入口(带 toast 副作用) */
  applyGlobal: (event: EngineEvent) => void
  /** 合并 REST 历史(并集去重,禁止 replace):LivePage 挂载回填用 */
  mergeHistory: (sessionId: string, transcripts: Transcript[], answers: Answer[]) => void
  reset: (sessionId: string | null) => void
  dismissToast: (id: number) => void
}

let toastSeq = 0
let audioFaultSeq = 0
let systemAudioFaultSeq = 0

const SESSION_STATUSES: readonly SessionStatus[] = ['idle', 'recording', 'ended']

/** 运行时校验 sessionState.status(服务端字符串未经类型约束)。 */
export function isValidSessionStatus(value: unknown): value is SessionStatus {
  return typeof value === 'string' && (SESSION_STATUSES as readonly string[]).includes(value)
}

/** 纯 reducer:EngineEvent → 状态补丁。 */
export function applyEngineEvent(state: LiveState, event: EngineEvent): Partial<LiveState> {
  switch (event.kind) {
    case 'connection': {
      return {
        phase: event.phase,
        note: event.note ?? undefined,
        synced: event.phase === 'ready' ? state.synced : false,
        sessionStatus:
          // 脆弱契约:note 文案由 Rust ws_client 直接透传服务端中文文案,前端
          // 以字符串匹配识别「会话已结束」。改动协议时两侧需同步(暂无法改 Rust)。
          event.phase === 'closed' && event.note === '会话已结束' ? 'ended' : state.sessionStatus
      }
    }
    case 'syncComplete':
      return { synced: true, lastEventId: Math.max(state.lastEventId, event.latestEventId) }
    case 'sessionState':
      // 非法 status 不写入,保持现有状态等待权威事件覆盖
      if (!isValidSessionStatus(event.status)) return state
      return {
        sessionStatus: event.status,
        radioMode: event.radioMode
      }
    case 'sessionEnded':
      return { sessionStatus: 'ended' }
    case 'serverMessage': {
      const type = (event as { type?: unknown }).type
      // 快速切换会话时,旧会话的迟到事件不得落入新会话的列表
      if (type === 'transcript') {
        const t = event as unknown as Transcript
        if (t.session_id !== state.sessionId) return state
        if (state.transcripts.some((x) => x.id === t.id)) return state
        const transcripts = [...state.transcripts, t].sort((a, b) => a.seq - b.seq)
        return {
          transcripts,
          partialTranscript:
            state.partialTranscript?.source === t.source ? null : state.partialTranscript
        }
      }
      if (type === 'transcript_partial') {
        const partial = event as unknown as PartialTranscript
        if (partial.session_id !== state.sessionId || typeof partial.text !== 'string') return state
        return {
          partialTranscript: partial.text.trim()
            ? { session_id: partial.session_id, source: partial.source, text: partial.text }
            : null
        }
      }
      if (type === 'answer') {
        const a = event as unknown as Answer
        if (a.session_id !== state.sessionId) return state
        if (state.answers.some((x) => x.id === a.id)) return state
        const streamingAnswers = { ...state.streamingAnswers }
        if (typeof a.request_id === 'string') delete streamingAnswers[a.request_id]
        return { answers: [...state.answers, a], streamingAnswers }
      }
      if (type === 'answer_stream') {
        const sessionId = event.session_id
        const question = event.question
        const text = event.text
        const channel = event.channel
        const requestId =
          typeof event.request_id === 'string' && event.request_id
            ? event.request_id
            : `legacy:${String(question)}`
        if (
          typeof sessionId !== 'string' ||
          sessionId !== state.sessionId ||
          typeof question !== 'string' ||
          typeof text !== 'string' ||
          (channel !== 'thinking' && channel !== 'answer')
        ) {
          return state
        }
        const previous = state.streamingAnswers[requestId]
        const answer: StreamingAnswer = {
          request_id: requestId,
          session_id: sessionId,
          question,
          answer:
            typeof event.answer === 'string'
              ? event.answer
              : channel === 'answer'
                ? text
                : previous?.answer ?? '',
          thinking:
            typeof event.thinking === 'string'
              ? event.thinking
              : channel === 'thinking'
                ? text
                : previous?.thinking ?? '',
          source: event.source === 'search+llm' ? 'search+llm' : ('llm' as AnswerSource),
          done: event.done === true
        }
        return {
          streamingAnswers: { ...state.streamingAnswers, [requestId]: answer }
        }
      }
      return state
    }
    case 'outbox':
      return { outbox: event.stats }
    case 'seqWatermark':
      return { seqWatermark: event.nextChunkSeq, seqWatermarkReady: true }
    case 'serverError':
      if (event.code === 'answer_generation_failed') {
        return { streamingAnswers: {} }
      }
      if (event.code === 'audio_processing_failed') {
        return { audioFault: { id: ++audioFaultSeq, message: event.message } }
      }
      if (event.code === 'system_audio_capture_failed') {
        return {
          systemAudioFault: { id: ++systemAudioFaultSeq, message: event.message }
        }
      }
      return state
    default:
      return state
  }
}

/**
 * 纯 reducer:历史回填与实时数据合并。
 *
 * 语义是**并集**而非 replace:历史请求在途时实时事件可能已经先到(连接是并行
 * 建立的),历史晚到不能覆盖/清空更新的实时数据;同 id 时实时数据为权威。
 * transcripts 按 id 去重、按 seq 升序;answers 按 id 去重、按 id 升序(与后端
 * get_answers 的 ORDER BY id 一致,与 applyEngineEvent 的去重规则同构)。
 * 跨会话载荷直接忽略,无新增数据时返回 null(避免无谓重渲染)。
 */
export function mergeHistory(
  state: Pick<LiveState, 'sessionId' | 'transcripts' | 'answers'>,
  sessionId: string,
  transcripts: Transcript[],
  answers: Answer[]
): Partial<LiveState> | null {
  if (state.sessionId !== sessionId) return null

  // id → 行。先放实时数据(同 id 时实时为权威),历史仅补缺。
  const transcriptMap = new Map(state.transcripts.map((t) => [t.id, t]))
  for (const t of transcripts) {
    if (t.session_id === sessionId && !transcriptMap.has(t.id)) transcriptMap.set(t.id, t)
  }
  const answerMap = new Map(state.answers.map((a) => [a.id, a]))
  for (const a of answers) {
    if (a.session_id === sessionId && !answerMap.has(a.id)) answerMap.set(a.id, a)
  }

  const nextTranscripts =
    transcriptMap.size === state.transcripts.length
      ? state.transcripts
      : [...transcriptMap.values()].sort((a, b) => a.seq - b.seq)
  const nextAnswers =
    answerMap.size === state.answers.length
      ? state.answers
      : // 后端按 id 升序返回;实时先到、历史后到时按 id 重排,保证展示顺序稳定
        [...answerMap.values()].sort((a, b) => a.id - b.id)

  if (
    nextTranscripts === state.transcripts &&
    nextAnswers === state.answers
  ) {
    return null
  }
  return { transcripts: nextTranscripts, answers: nextAnswers }
}

export const useLiveStore = create<LiveState>((set, get) => ({
  sessionId: null,
  phase: 'idle',
  sessionStatus: '',
  radioMode: 'pc',
  transcripts: [],
  partialTranscript: null,
  answers: [],
  streamingAnswers: {},
  lastEventId: 0,
  synced: false,
  outbox: null,
  seqWatermark: 0,
  seqWatermarkReady: false,
  audioFault: null,
  systemAudioFault: null,
  toasts: [],
  apply: (event) =>
    set((state) => {
      const patch = applyEngineEvent(state, event)
      return patch
    }),
  applyGlobal: (event) => {
    // toast 副作用(仅错误类);同 message 去重,批量失败不刷屏
    if (event.kind === 'serverError') {
      const kind =
        event.code === 'paid_usage_limited' ||
        event.code === 'audio_backpressure' ||
        event.code === 'reconcile_warning' ||
        event.code === 'invalid_session_state' ||
        (event.code === 'audio_processing_failed' && event.message.includes('未配置'))
          ? 'warning'
          : 'error'
      const message =
        event.code === 'paid_usage_limited'
          ? `付费服务额度受限,${event.retryAfterSeconds ?? 30} 秒后自动重试`
          : event.code === 'invalid_session_state'
            ? '会话状态已变化(可能已结束或服务重启),请返回列表重新进入'
            : event.code === 'audio_processing_failed' && event.message.includes('未配置')
              ? `${event.message} → 打开「设置」页填写即可`
              : event.code === 'audio_processing_failed'
                ? `${event.message}；已停止采集并取消待处理音频`
                : event.message
      const duplicate = get().toasts.some((t) => t.message === message)
      if (!duplicate) {
        const id = ++toastSeq
        set((s) => ({ toasts: [...s.toasts, { id, kind, message }] }))
        setTimeout(() => get().dismissToast(id), 4500)
      }
    }
    set((state) => applyEngineEvent(state, event))
  },
  mergeHistory: (sessionId, transcripts, answers) =>
    set((state) => {
      const patch = mergeHistory(state, sessionId, transcripts, answers)
      return patch ?? {}
    }),
  reset: (sessionId) =>
    set({
      sessionId,
      phase: 'idle',
      sessionStatus: '',
      radioMode: 'pc',
      transcripts: [],
      partialTranscript: null,
      answers: [],
      streamingAnswers: {},
      lastEventId: 0,
      synced: false,
      outbox: null,
      seqWatermark: 0,
      seqWatermarkReady: false,
      audioFault: null,
      systemAudioFault: null,
      toasts: []
    }),
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }))
}))
