import { create } from 'zustand'
import type { Session } from '../shared/types'
import { api } from '../api/bridge'
import { emitToast, errorMessage } from '../shared/errors'

interface SessionsState {
  sessions: Session[]
  loading: boolean
  load: (limit?: number, offset?: number) => Promise<void>
  create: (title: string) => Promise<Session>
  remove: (id: string) => Promise<void>
}

export const useSessionsStore = create<SessionsState>((set, get) => ({
  sessions: [],
  loading: false,
  load: async (limit = 50, offset = 0) => {
    set({ loading: true })
    try {
      const page = await api.sessions.list(limit, offset)
      set((state) => ({
        sessions: offset === 0 ? page : [...state.sessions, ...page]
      }))
    } catch (err) {
      // load 全部为 void 调用;不 catch 会变成 unhandled rejection 且空列表误导用户
      emitToast('error', `加载会话列表失败：${errorMessage(err)}`)
    } finally {
      set({ loading: false })
    }
  },
  create: async (title) => {
    const session = await api.sessions.create(title)
    set({ sessions: [session, ...get().sessions] })
    return session
  },
  remove: async (id) => {
    await api.sessions.delete(id)
    set({ sessions: get().sessions.filter((s) => s.id !== id) })
  }
}))
