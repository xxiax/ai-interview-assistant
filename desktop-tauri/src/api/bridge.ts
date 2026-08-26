/**
 * Tauri 命令桥接层：统一 React 与 Rust core 的调用形状。
 * Rust 侧命令见 src-tauri/src/lib.rs。
 */
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import type {
  Answer,
  ConfigItem,
  ConfigType,
  EngineEvent,
  OutboxStats,
  Review,
  Session,
  Transcript
} from '../shared/types'

export interface AppSettings {
  serverUrl: string
  hasToken: boolean
}

export interface ConnectivityResult {
  ok: boolean
  reason?: string
}

export interface GlobalPrompt {
  prompt: string
}

export interface AudioChunkMeta {
  chunkId: string
  chunkSeq: number
  capturedAt: string
  durationMs: number
}

export const api = {
  settings: {
    get: () => invoke<AppSettings>('settings_get'),
    set: (input: { serverUrl: string; token?: string }) =>
      invoke<AppSettings>('settings_set', { serverUrl: input.serverUrl, token: input.token }),
    check: () => invoke<ConnectivityResult>('settings_check'),
    prompt: {
      get: () => invoke<GlobalPrompt>('settings_prompt_get'),
      set: (prompt: string) => invoke<GlobalPrompt>('settings_prompt_set', { prompt })
    }
  },
  sessions: {
    create: (title: string) => invoke<Session>('sessions_create', { title }),
    list: (limit: number, offset: number) => invoke<Session[]>('sessions_list', { limit, offset }),
    get: (id: string) => invoke<Session>('sessions_get', { id }),
    end: (id: string) => invoke<Session>('sessions_end', { id }),
    delete: (id: string) => invoke<void>('sessions_delete', { id })
  },
  live: {
    connect: (sessionId: string) => invoke<boolean>('live_connect', { sessionId }),
    disconnect: () => invoke<void>('live_disconnect'),
    startSession: (radioMode: string) => invoke<boolean>('live_start_session', { radioMode }),
    setRadioMode: (mode: string) => invoke<boolean>('live_set_radio_mode', { mode }),
    regenerate: (question: string, useSearch: boolean) =>
      invoke<boolean>('live_regenerate', { question, useSearch }),
    endSession: () => invoke<boolean>('live_end_session')
  },
  audio: {
    startSystem: () => invoke<boolean>('system_audio_start'),
    stopSystem: () => invoke<void>('system_audio_stop'),
    chunk: (meta: AudioChunkMeta, data: ArrayBuffer) =>
      invoke<boolean>('audio_chunk', {
        chunkId: meta.chunkId,
        chunkSeq: meta.chunkSeq,
        capturedAt: meta.capturedAt,
        durationMs: meta.durationMs,
        data: Array.from(new Uint8Array(data))
      })
  },
  outbox: {
    snapshot: () => invoke<OutboxStats>('outbox_snapshot'),
    reconcile: () => invoke<void>('outbox_reconcile'),
    setCaptureActive: (active: boolean, reason?: 'capture_stopped' | 'source_disabled') =>
      invoke<void>('outbox_set_capture_active', { active, reason })
  },
  history: {
    transcripts: (sessionId: string) => invoke<Transcript[]>('history_transcripts', { sessionId }),
    answers: (sessionId: string) => invoke<Answer[]>('history_answers', { sessionId }),
    reviews: (sessionId: string) => invoke<Review[]>('history_reviews', { sessionId }),
    generateReview: (sessionId: string, useSearch: boolean) =>
      invoke<Review>('history_generate_review', { sessionId, useSearch })
  },
  configs: {
    list: (type: ConfigType) => invoke<ConfigItem[]>('configs_list', { configType: type }),
    save: (type: ConfigType, body: unknown) =>
      invoke<ConfigItem>('configs_save', { configType: type, body }),
    activate: (type: ConfigType, id: number) =>
      invoke<ConfigItem>('configs_activate', { configType: type, id }),
    delete: (type: ConfigType, id: number) =>
      invoke<void>('configs_delete', { configType: type, id }),
    /** 拉取 provider 模型列表;编辑已有配置且 apiKey 留空时仅在 URL 未变化时复用已存密钥。 */
    fetchModels: (baseUrl: string, apiKey: string, authField: string) =>
      invoke<{ models: string[] }>('llm_fetch_models', { baseUrl, apiKey, authField })
  },
  events: {
    /** 订阅引擎事件;返回取消函数。 */
    on: (cb: (event: EngineEvent) => void): Promise<UnlistenFn> =>
      listen<EngineEvent>('engine:event', (e) => cb(e.payload))
  }
}
