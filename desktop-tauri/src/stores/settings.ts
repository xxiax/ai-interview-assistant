import { create } from 'zustand'
import { api, type AppSettings, type ConnectivityResult } from '../api/bridge'
import { emitToast, errorMessage } from '../shared/errors'

interface SettingsState {
  settings: AppSettings | null
  loading: boolean
  checking: boolean
  lastCheck: ConnectivityResult | null
  load: () => Promise<void>
  save: (input: { token?: string }) => Promise<void>
  check: () => Promise<ConnectivityResult>
}

export const useSettingsStore = create<SettingsState>((set) => ({
  settings: null,
  loading: false,
  checking: false,
  lastCheck: null,
  load: async () => {
    set({ loading: true })
    try {
      const settings = await api.settings.get()
      set({ settings })
    } catch (err) {
      // load 由组件 void 调用;不 catch 会变成 unhandled rejection 且空表单误导用户
      emitToast('error', `读取设置失败：${errorMessage(err)}`)
    } finally {
      set({ loading: false })
    }
  },
  save: async (input) => {
    const settings = await api.settings.set(input)
    set({ settings, lastCheck: null })
  },
  check: async () => {
    set({ checking: true })
    try {
      const result = await api.settings.check()
      set({ lastCheck: result })
      return result
    } finally {
      set({ checking: false })
    }
  }
}))
