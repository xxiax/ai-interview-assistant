/**
 * 后端 REST / WebSocket v1 协议类型，适配 Tauri 事件载荷。
 */

export type RadioMode = 'pc' | 'mobile' | 'both'
export type AudioSource = 'pc' | 'mobile'
export type SessionStatus = 'idle' | 'recording' | 'ended'
export type AnswerSource = 'llm' | 'search+llm'
export type ConfigType = 'llm' | 'search' | 'asr' | 'network'

export interface Session {
  id: string
  title: string
  status: SessionStatus
  radio_mode: RadioMode
  created_at: string
  ended_at: string | null
  /** 岗位 JD：会话级答题背景，缺省时后端生成通用答案。 */
  job_description?: string | null
  /** 简历：会话级答题背景。 */
  resume?: string | null
}

export interface Transcript {
  id: number
  session_id: string
  source: AudioSource
  text: string
  timestamp: string
  seq: number
  chunk_id?: string | null
  chunk_seq?: number | null
  captured_at?: string | null
}

export interface PartialTranscript {
  session_id: string
  source: AudioSource
  text: string
}

export interface Answer {
  id: number
  session_id: string
  question: string
  answer: string
  // 后端 db.py VALID_ANSWER_SOURCES = {"llm", "search+llm"}(写入时已校验)
  source: AnswerSource
  created_at: string
  request_id?: string
  thread_id?: string
  revision?: number
}

export interface StreamingAnswer {
  request_id: string
  thread_id?: string
  revision: number
  session_id: string
  question: string
  answer: string
  source: AnswerSource
  done: boolean
  started: boolean
  failed: boolean
  /** 被同线程更新 revision 取代：内容冻结保留（catch-up swap），不算失败。 */
  superseded?: boolean
}

export interface Review {
  id: number
  session_id: string
  content: string
  // 同 Answer.source:后端 routes_review.py 仅写 "llm" | "search+llm"
  source: AnswerSource
  created_at: string
}

export interface ConfigItem {
  id: number
  type: ConfigType
  name: string
  data: Record<string, unknown>
  is_active: boolean
  secret_configured: boolean
}

// ---------- 引擎事件(Rust → 前端,tag=kind camelCase) ----------

export type ConnectionPhase =
  | 'idle'
  | 'connecting'
  | 'authenticating'
  | 'synchronizing'
  | 'ready'
  | 'reconnecting'
  | 'closed'

/**
 * Rust 侧实时链路快照（live_runtime_state 命令）。
 *
 * 引擎事件只在发生瞬间广播，悬浮窗是懒创建的，它打开前的事件（比如已经
 * 开始的录制）永远收不到。悬浮窗挂载时拉这份快照播种，之后的增量照常走
 * engine:event。字段为 null 表示 Rust 侧未知/引擎从未启动。
 */
export interface LiveRuntimeState {
  phase: string | null
  sessionId: string | null
  sessionStatus: string | null
  radioMode: string | null
  captureActive: boolean
}

export interface OutboxStats {
  captured: number
  sending: number
  queued: number
  retryable: number
  terminal: number
  released: number
  next_chunk_seq: number
}

export type EngineEvent =
  | { kind: 'connection'; phase: ConnectionPhase; note?: string | null; retryAfterMs?: number | null }
  | { kind: 'syncComplete'; latestEventId: number }
  | { kind: 'sessionState'; status: string; radioMode: RadioMode }
  | { kind: 'sessionEnded' }
  /** 本机采集门翻转（Rust SetCaptureActive 处理后推送）；悬浮窗区分「会话在录」与「本机在采」。 */
  | { kind: 'captureState'; active: boolean }
  | { kind: 'serverMessage'; [key: string]: unknown }
  | {
      kind: 'serverError'
      code: string
      message: string
      retryAfterSeconds?: number | null
      chunkId?: string | null
    }
  | { kind: 'outbox'; stats: OutboxStats }
  | { kind: 'seqWatermark'; nextChunkSeq: number }

/**
 * 悬浮提词窗状态。权威副本在 Rust（OverlayStateHandle），
 * 每个 overlay_* 命令都回传最新值，前端不要自己推算。
 */
export interface OverlayState {
  visible: boolean
  /** 键盘焦点是否在悬浮窗上；窗口从不主动抢焦，只有用户点击才为 true。 */
  focused: boolean
  /** 鼠标穿透：true 时点击穿到底下的会议窗口。 */
  passthrough: boolean
  /** 穿透开启时按住 Ctrl 临时获得鼠标交互（Rust 轮询 GetAsyncKeyState），松开恢复穿透。 */
  ctrlInteractive: boolean
  /** 对 OS 截屏/录屏/共享隐身（Windows WDA_EXCLUDEFROMCAPTURE）。 */
  contentProtected: boolean
  alwaysOnTop: boolean
  /** 0.25 - 1.0，由前端 CSS 应用。 */
  opacity: number
  /** 是否已收起成屏幕顶部居中的细条。从任意位置都能收起。 */
  collapsed: boolean
}
