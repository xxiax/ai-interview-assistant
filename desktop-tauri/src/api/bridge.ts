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
  LiveRuntimeState,
  OutboxStats,
  OverlayState,
  Review,
  Session,
  Transcript
} from '../shared/types'

export interface AppSettings {
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
    set: (input: { token?: string }) => invoke<AppSettings>('settings_set', { token: input.token }),
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
    /** 写入会话级岗位 JD 与简历；空串表示清空。任何会话状态都可写。 */
    setContext: (id: string, jobDescription: string, resume: string) =>
      invoke<Session>('sessions_set_context', { id, jobDescription, resume }),
    end: (id: string) => invoke<Session>('sessions_end', { id }),
    delete: (id: string) => invoke<void>('sessions_delete', { id })
  },
  live: {
    connect: (sessionId: string) => invoke<boolean>('live_connect', { sessionId }),
    disconnect: () => invoke<void>('live_disconnect'),
    startSession: (radioMode: string) => invoke<boolean>('live_start_session', { radioMode }),
    /** 当前实时链路快照：晚挂载的悬浮窗播种用（错过的事件补不回来，只能问）。 */
    runtimeState: () => invoke<LiveRuntimeState>('live_runtime_state'),
    setRadioMode: (mode: string) => invoke<boolean>('live_set_radio_mode', { mode }),
    /**
     * 重新生成。带 threadId 时后端 revision+1、答案流回同一张卡；
     * 不带时是独立手动提问，立即入库。
     */
    regenerate: (question: string, useSearch: boolean, threadId?: string) =>
      invoke<boolean>('live_regenerate', { question, useSearch, threadId }),
    /**
     * 笔试辅助：抓当前屏幕交给后端多模态解题。
     *
     * 返回 false 表示还没连上服务（不是抓屏失败）；抓屏本身失败会 reject。
     * 截图只在内存里流转，不落盘也不入库，答案按普通 answer 事件回来。
     */
    solveScreenshot: (note?: string) => invoke<boolean>('live_solve_screenshot', { note }),
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
      listen<EngineEvent>('engine:event', (e) => cb(e.payload)),
    /**
     * 订阅 Rust 主动发出的提示。
     *
     * 必须有这一条：全局热键的失败只发生在 Rust 侧（没有对应的 invoke 调用可以
     * reject），它 `app.emit("app-toast", ...)` 出来，前端不订阅就等于静默失败。
     */
    onToast: (cb: (payload: { kind: string; message: string }) => void): Promise<UnlistenFn> =>
      listen<{ kind: string; message: string }>('app-toast', (e) => cb(e.payload))
  },
  /**
   * 悬浮提词窗。全部走 Rust 命令而不是 `@tauri-apps/api/window`：
   * capabilities 只授予窗口 getter（没有 allow-set-*），前端拿不到 setter 权限，
   * 也顺带把攻击面收窄成这里列出的几个动作。
   */
  overlay: {
    snapshot: () => invoke<OverlayState>('overlay_snapshot'),
    show: () => invoke<OverlayState>('overlay_show'),
    hide: () => invoke<OverlayState>('overlay_hide'),
    toggle: () => invoke<OverlayState>('overlay_toggle'),
    setPassthrough: (passthrough: boolean) =>
      invoke<OverlayState>('overlay_set_passthrough', { passthrough }),
    /** 共享隐身；只挡 OS 截屏/录屏 API，挡不住采集卡和手机拍屏。 */
    setContentProtected: (protectedFlag: boolean) =>
      invoke<OverlayState>('overlay_set_content_protected', { protected: protectedFlag }),
    setAlwaysOnTop: (onTop: boolean) =>
      invoke<OverlayState>('overlay_set_always_on_top', { onTop }),
    setOpacity: (opacity: number) => invoke<OverlayState>('overlay_set_opacity', { opacity }),
    /** 收起成屏幕顶部居中的细条；从任意窗口位置都能收起。 */
    collapse: () => invoke<OverlayState>('overlay_collapse'),
    expand: () => invoke<OverlayState>('overlay_expand'),
    /** 订阅热键触发的状态变更；主窗口与悬浮窗都要订阅，两边各有独立 JS 上下文。 */
    onState: (cb: (state: OverlayState) => void): Promise<UnlistenFn> =>
      listen<OverlayState>('overlay:state', (e) => cb(e.payload)),
    /**
     * Ctrl+Alt+Z（开启/暂停录制）。Rust 只广播事件，动作在悬浮窗
     * webview 里执行（没录就 开会话 → 系统采集 → 上传门禁；在录就
     * 暂停或恢复：只停/起采集与门禁，不结束会话），反馈就地显示。
     */
    onToggleRecordingHotkey: (cb: () => void): Promise<UnlistenFn> =>
      listen('overlay:toggle-recording', () => cb())
  }
}
