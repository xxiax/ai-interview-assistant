/**
 * 后端 REST / WebSocket v1 协议类型(与 desktop/src/shared/protocol.ts 同源,适配 Tauri 事件载荷)。
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
  thinking?: string | null
  // 后端 db.py VALID_ANSWER_SOURCES = {"llm", "search+llm"}(写入时已校验)
  source: AnswerSource
  created_at: string
  request_id?: string
}

export interface StreamingAnswer {
  request_id: string
  session_id: string
  question: string
  answer: string
  thinking: string
  source: AnswerSource
  done: boolean
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
