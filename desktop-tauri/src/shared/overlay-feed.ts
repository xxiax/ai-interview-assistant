/**
 * 悬浮提词窗自己的事件归约。
 *
 * 为什么不复用 `stores/live.ts`：第二个 webview 有独立 JS 上下文，Zustand store
 * 是另一份实例，`sessionId` 永远是 null，`applyEngineEvent` 的跨会话过滤会把所有
 * 事件丢掉。悬浮窗也不需要历史回填和 outbox 统计，它只要"当前在问什么 + 当前答案"。
 *
 * 因此这里的规则不是"过滤掉别的会话"，而是"跟随最新会话"：
 * 收到不同 session_id 的载荷就整体切过去，旧会话的内容清空。
 */
import type {
  Answer,
  AnswerSource,
  ConnectionPhase,
  EngineEvent,
  LiveRuntimeState,
  PartialTranscript,
  RadioMode,
  SessionStatus,
  StreamingAnswer
} from './types'

/**
 * 保留的流式答案段数上限。面试一小时能攒出几百段，悬浮窗只看最近的，
 * 留太多既费内存又拖慢渲染。完整记录在主窗口和数据库里。
 */
export const MAX_OVERLAY_STREAM_ENTRIES = 60

export interface OverlayFeedState {
  sessionId: string | null
  phase: ConnectionPhase
  sessionStatus: SessionStatus | ''
  /** 当前收音模式（sessionState 事件带上）；徽标用它区分手机收音与本机采集。 */
  radioMode: RadioMode | ''
  /** 本机采集门是否开着（captureState 事件）。会话 recording ≠ 本机在采。 */
  captureOn: boolean
  /** 当前累计 partial（面试官正在问的那句）。 */
  partial: PartialTranscript | null
  /** 按 request_id 存的流式答案段，插入顺序即到达顺序。 */
  streaming: Record<string, StreamingAnswer>
  /** 已落库的最终答案，用来在卡上标"已入库"。 */
  answers: Answer[]
  /**
   * 已发出、还没等到任何 answer_stream 帧的手动提问。发送成功即入列渲染成
   * "正在思考"卡；同文本的流帧（含 started 空帧/失败帧）到达即摘除。
   */
  pending: OverlayPendingQuestion[]
}

export interface OverlayPendingQuestion {
  id: number
  question: string
}

export const initialOverlayFeed: OverlayFeedState = {
  sessionId: null,
  phase: 'idle',
  sessionStatus: '',
  radioMode: '',
  captureOn: false,
  partial: null,
  streaming: {},
  answers: [],
  pending: []
}

const SESSION_STATUSES: readonly SessionStatus[] = ['idle', 'recording', 'ended']

/** 合法连接阶段（overlay:seed 的 Rust 快照字符串要过这道校验）。 */
const CONNECTION_PHASES: readonly ConnectionPhase[] = [
  'idle',
  'connecting',
  'authenticating',
  'synchronizing',
  'ready',
  'reconnecting',
  'closed'
]

/** pending 的自增 id（与内容无关，只做 React key）。 */
let pendingSeq = 0

/**
 * 「清空当前回答」动作。和引擎事件走同一个 reducer，因为清空必须和事件到达
 * 串行化：单独用一个 `useState` 的话，清空和一个正在流的 `answer_stream`
 * 可能基于同一份旧 state 各算一次，清空就会被下一个 token 覆盖回来。
 *
 * `kind` 用 `overlay:` 前缀，避免和后端可能新增的事件类型撞名。
 * - `overlay:asked`：手动提问发送成功，立刻挂一张"正在思考"卡。
 * - `overlay:seed`：挂载时播种 Rust 运行时快照（打开悬浮窗之前的事件收不到，
 *   只能主动问一次；见 api.live.runtimeState）。
 */
export type OverlayFeedAction =
  | EngineEvent
  | { kind: 'overlay:clear' }
  | { kind: 'overlay:asked'; question: string }
  | { kind: 'overlay:seed'; snapshot: LiveRuntimeState }

function trim(streaming: Record<string, StreamingAnswer>): Record<string, StreamingAnswer> {
  const keys = Object.keys(streaming)
  if (keys.length <= MAX_OVERLAY_STREAM_ENTRIES) return streaming
  const kept = keys.slice(keys.length - MAX_OVERLAY_STREAM_ENTRIES)
  const next: Record<string, StreamingAnswer> = {}
  for (const key of kept) next[key] = streaming[key]
  return next
}

/**
 * 切会话时清空提词内容，避免上一场的答案留在窗口里。
 *
 * 会话状态也一并清掉：那是上一场的状态，新会话还没报告自己的状态。
 * 沿用旧值会让悬浮窗替"整个项目"报状态（比如上一场的录制中一直挂着），
 * 它只该跟当前连接的面试走。
 */
function adopt(state: OverlayFeedState, sessionId: string): OverlayFeedState {
  if (state.sessionId === sessionId) return state
  return { ...initialOverlayFeed, sessionId, phase: state.phase }
}

export function applyOverlayEvent(
  state: OverlayFeedState,
  event: OverlayFeedAction
): OverlayFeedState {
  switch (event.kind) {
    case 'overlay:clear':
      // 只清内容，保留连接/会话/会话 id：清空是"擦掉屏幕"，不是断开或换会话。
      // 保留 sessionId 尤其重要，否则下一个事件会被 adopt() 当成切会话再清一次。
      return {
        ...state,
        partial: null,
        streaming: {},
        answers: [],
        pending: []
      }
    case 'overlay:asked':
      return {
        ...state,
        pending: [...state.pending, { id: ++pendingSeq, question: event.question }]
      }
    case 'overlay:seed': {
      // 挂载时执行一次。快照字符串过校验再落地（Rust 侧也是字符串透传），
      // 非法值一律回退为"未知"，别让脏数据顶掉合法状态。
      const s = event.snapshot
      const phase = CONNECTION_PHASES.includes(s.phase as ConnectionPhase)
        ? (s.phase as ConnectionPhase)
        : 'idle'
      const sessionStatus = SESSION_STATUSES.includes(s.sessionStatus as SessionStatus)
        ? (s.sessionStatus as SessionStatus)
        : ''
      const radioMode = s.radioMode === 'pc' || s.radioMode === 'mobile' ? s.radioMode : ''
      return {
        ...state,
        sessionId: s.sessionId ?? null,
        phase,
        sessionStatus,
        radioMode,
        captureOn: s.captureActive
      }
    }
    case 'connection':
      // 引擎彻底断开（离开实时页 / 会话被替换）后"录制中"不再成立：悬浮窗
      // 只跟当前连接的面试走，不能拿着上一场的状态一直报。重连中的短暂
      // 断线（reconnecting）不算断开，状态要保住。
      return event.phase === 'closed' || event.phase === 'idle'
        ? { ...state, phase: event.phase, sessionStatus: '', captureOn: false, pending: [] }
        : { ...state, phase: event.phase }
    case 'sessionState':
      if (!SESSION_STATUSES.includes(event.status as SessionStatus)) return state
      return { ...state, sessionStatus: event.status as SessionStatus, radioMode: event.radioMode }
    case 'captureState':
      return { ...state, captureOn: event.active }
    case 'sessionEnded':
      return { ...state, sessionStatus: 'ended' }
    case 'serverMessage': {
      const type = (event as { type?: unknown }).type
      if (type === 'transcript_partial') {
        const partial = event as unknown as PartialTranscript
        if (typeof partial.session_id !== 'string' || typeof partial.text !== 'string') return state
        const next = adopt(state, partial.session_id)
        return {
          ...next,
          partial: partial.text.trim()
            ? { session_id: partial.session_id, source: partial.source, text: partial.text }
            : null
        }
      }
      if (type === 'transcript') {
        // 一句话说完了：清掉 partial，答案卡自己留着。
        const sessionId = (event as { session_id?: unknown }).session_id
        if (typeof sessionId !== 'string') return state
        const next = adopt(state, sessionId)
        return { ...next, partial: null }
      }
      if (type === 'answer') {
        const answer = event as unknown as Answer
        if (typeof answer.session_id !== 'string' || typeof answer.id !== 'number') return state
        const next = adopt(state, answer.session_id)
        if (next.answers.some((item) => item.id === answer.id)) return next
        return { ...next, answers: [...next.answers, answer] }
      }
      if (type === 'answer_stream') {
        const sessionId = event.session_id
        const question = event.question
        const fullText = event.answer
        const channel = event.channel
        if (
          typeof sessionId !== 'string' ||
          typeof question !== 'string' ||
          typeof fullText !== 'string' ||
          (channel !== 'thinking' && channel !== 'answer')
        ) {
          return state
        }
        // 思考过程通道已下线，直接丢弃（与主窗口一致）。
        if (channel === 'thinking') return state
        const requestId =
          typeof event.request_id === 'string' && event.request_id
            ? event.request_id
            : `legacy:${question}`
        const next = adopt(state, sessionId)
        const previous = next.streaming[requestId]
        // 兼容旧后端的 superseded 终止帧。当前后端各 revision 互不取消；
        // 若收到旧帧仍冻结该段并保留半截答案。
        if (event.superseded === true) {
          if (!previous) return next
          const streaming = { ...next.streaming }
          streaming[requestId] = {
            ...previous,
            done: true,
            failed: false,
            superseded: true
          }
          return { ...next, streaming }
        }
        const failed = event.failed === true
        const done = event.done === true
        const started = event.started === true
        // delta-only 契约:token 帧只带 delta(全文恒为空串),全文只在 done 帧
        // 可信;done+failed 且全文为空时保留已流出内容;started 空帧开新卡清空文本。
        const delta = typeof event.delta === 'string' ? event.delta : ''
        const answer = done
          ? fullText || (failed ? previous?.answer ?? '' : '')
          : started
            ? ''
            : (previous?.answer ?? '') + delta
        const entry: StreamingAnswer = {
          request_id: requestId,
          thread_id:
            typeof event.thread_id === 'string' && event.thread_id ? event.thread_id : undefined,
          revision:
            typeof event.revision === 'number' && Number.isInteger(event.revision)
              ? event.revision
              : 1,
          session_id: sessionId,
          question,
          answer,
          source: event.source === 'search+llm' ? 'search+llm' : ('llm' as AnswerSource),
          done,
          started,
          failed
        }
        // catch-up swap：新 revision 的帧不再删除旧 revision 的段——旧段还在
        // 流式输出时被删会让用户眼前的答案凭空消失。全部保留，渲染层
        // （pickDisplayVersion）挑「未被取代里答案最长」的那段展示：新版
        // 追平长度即自然接管，旧版收尾前始终兜底可读。
        //
        // 手动提问的 pending 卡在这里交棒：started 空帧在生成一开始就到，
        // 同文本的 pending 即刻摘除，"正在思考"无缝变成流式卡。
        return {
          ...next,
          streaming: trim({ ...next.streaming, [requestId]: entry }),
          pending: next.pending.filter((p) => p.question !== question)
        }
      }
      return state
    }
    default:
      return state
  }
}
