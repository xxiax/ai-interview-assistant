# PC 端（Electron + React）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 AI 面试助手的 PC 端桌面应用：Electron + React + Ant Design + Tailwind CSS，包含收音、实时转写/答案显示、会话控制、配置管理。

**Architecture:** Electron 主进程负责窗口管理，渲染进程运行 React 应用。React 应用通过 WebSocket 连接后端，通过 MediaRecorder 采集麦克风音频并分片上传。页面结构：侧边栏导航（面试/历史会话/设置）+ 主内容区。

**Tech Stack:** Electron, React 18, Vite, Ant Design 5, Tailwind CSS, WebSocket, MediaRecorder

## Global Constraints

- Node 22+（本机 v22.20.0）
- 前端框架：React 18 + Vite
- UI：Ant Design 5 + Tailwind CSS
- 后端 API 地址通过环境变量 `VITE_API_BASE_URL` 配置（默认 `http://localhost:8000`）
- 所有代码注释使用中文
- 每个任务结束必须提交 git

---

### Task 1: Electron + React + Vite 脚手架

**Files:**
- Create: `desktop/package.json`
- Create: `desktop/vite.config.ts`
- Create: `desktop/tsconfig.json`
- Create: `desktop/index.html`
- Create: `desktop/electron/main.ts`
- Create: `desktop/electron/preload.ts`
- Create: `desktop/src/main.tsx`
- Create: `desktop/src/App.tsx`
- Create: `desktop/src/index.css`
- Create: `desktop/.env.example`

**Interfaces:**
- Consumes: 无
- Produces: 可启动的 Electron 应用，渲染进程加载 React

- [ ] **Step 1: 创建 package.json**

```json
{
  "name": "ai-interview-desktop",
  "version": "0.1.0",
  "private": true,
  "main": "dist-electron/main.js",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "electron:dev": "concurrently -k \"vite\" \"wait-on tcp:5173 && electron .\"",
    "electron:build": "npm run build && electron-builder"
  },
  "dependencies": {
    "antd": "^5.22.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-router-dom": "^6.28.0"
  },
  "devDependencies": {
    "@types/react": "^18.3.12",
    "@types/react-dom": "^18.3.1",
    "@vitejs/plugin-react": "^4.3.4",
    "autoprefixer": "^10.4.20",
    "concurrently": "^9.1.0",
    "electron": "^33.2.0",
    "electron-builder": "^25.1.8",
    "postcss": "^8.4.49",
    "tailwindcss": "^3.4.15",
    "typescript": "^5.6.3",
    "vite": "^5.4.11",
    "wait-on": "^8.0.1"
  }
}
```

- [ ] **Step 2: 创建 vite.config.ts**

```ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5173,
  },
})
```

- [ ] **Step 3: 创建 tsconfig.json**

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true
  },
  "include": ["src"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
```

- [ ] **Step 4: 创建 tsconfig.node.json**

```json
{
  "compilerOptions": {
    "composite": true,
    "skipLibCheck": true,
    "module": "ESNext",
    "moduleResolution": "bundler",
    "allowSyntheticDefaultImports": true
  },
  "include": ["vite.config.ts"]
}
```

- [ ] **Step 5: 创建 index.html**

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>AI 面试助手</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 6: 创建 electron/main.ts**

```ts
import { app, BrowserWindow } from 'electron'
import path from 'path'

function createWindow() {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  // 开发模式加载 Vite dev server，生产模式加载打包文件
  if (process.env.VITE_DEV_SERVER_URL) {
    win.loadURL(process.env.VITE_DEV_SERVER_URL)
  } else {
    win.loadFile(path.join(__dirname, '../dist/index.html'))
  }
}

app.whenReady().then(() => {
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
```

- [ ] **Step 7: 创建 electron/preload.ts**

```ts
import { contextBridge } from 'electron'

contextBridge.exposeInMainWorld('electronAPI', {
  platform: process.platform,
})
```

- [ ] **Step 8: 创建 src/main.tsx**

```tsx
import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ConfigProvider>
  </React.StrictMode>,
)
```

- [ ] **Step 9: 创建 src/App.tsx**

```tsx
import { Layout, Menu } from 'antd'
import { Routes, Route, useNavigate, useLocation } from 'react-router-dom'
import { AudioOutlined, HistoryOutlined, SettingOutlined } from '@ant-design/icons'

const { Sider, Content } = Layout

function App() {
  const navigate = useNavigate()
  const location = useLocation()

  const menuItems = [
    { key: '/', icon: <AudioOutlined />, label: '面试' },
    { key: '/history', icon: <HistoryOutlined />, label: '历史会话' },
    { key: '/settings', icon: <SettingOutlined />, label: '设置' },
  ]

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="dark">
        <div style={{ color: '#fff', textAlign: 'center', padding: '16px', fontSize: '16px', fontWeight: 'bold' }}>
          AI 面试助手
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Content style={{ padding: '24px' }}>
        <Routes>
          <Route path="/" element={<div>面试页面（待实现）</div>} />
          <Route path="/history" element={<div>历史会话页面（待实现）</div>} />
          <Route path="/settings" element={<div>设置页面（待实现）</div>} />
        </Routes>
      </Content>
    </Layout>
  )
}

export default App
```

- [ ] **Step 10: 创建 src/index.css**

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
}
```

- [ ] **Step 11: 创建 tailwind.config.js**

```js
/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {},
  },
  plugins: [],
}
```

- [ ] **Step 12: 创建 postcss.config.js**

```js
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
```

- [ ] **Step 13: 创建 .env.example**

```env
# 后端 API 地址
VITE_API_BASE_URL=http://localhost:8000
```

- [ ] **Step 14: 安装依赖并启动验证**

```bash
cd desktop
npm install
npm run dev
# 浏览器访问 http://localhost:5173 应显示侧边栏 + 三个页面
```

- [ ] **Step 15: 提交**

```bash
git add desktop/
git commit -m "feat: Electron + React + Vite scaffold with antd and tailwind"
```

---

### Task 2: API 客户端封装

**Files:**
- Create: `desktop/src/api/client.ts`
- Create: `desktop/src/api/sessions.ts`
- Create: `desktop/src/api/configs.ts`
- Create: `desktop/src/api/types.ts`

**Interfaces:**
- Consumes: 后端 REST API（见后端计划 Task 5、6）
- Produces:
  - `apiClient`（fetch 封装，自动带 base URL）
  - `createSession(title: string) -> Session`
  - `getSession(id: string) -> Session`
  - `endSession(id: string) -> Session`
  - `getTranscripts(id: string) -> Transcript[]`
  - `getAnswers(id: string) -> Answer[]`
  - `getConfigs(type: string) -> Config[]`
  - `saveConfig(type: string, name: string, data: object, isActive: boolean) -> Config`
  - `activateConfig(type: string, id: number) -> Config`
  - 类型：`Session`, `Transcript`, `Answer`, `Config`

- [ ] **Step 1: 创建 types.ts**

```ts
// 与后端数据模型对应的类型定义

export interface Session {
  id: string
  title: string
  status: 'idle' | 'recording' | 'ended'
  radio_mode: 'pc' | 'mobile' | 'both'
  created_at: string
  ended_at: string | null
}

export interface Transcript {
  id: number
  session_id: string
  source: 'pc' | 'mobile'
  text: string
  timestamp: string
  seq: number
}

export interface Answer {
  id: number
  session_id: string
  question: string
  answer: string
  source: 'llm' | 'search+llm'
  created_at: string
}

export interface Config {
  id: number
  type: 'llm' | 'search'
  name: string
  data: Record<string, string>
  is_active: boolean
}

// WebSocket 消息类型
export type WSMessage =
  | { type: 'transcript'; session_id: string; source: 'pc' | 'mobile'; text: string; seq: number }
  | { type: 'answer'; session_id: string; question: string; answer: string }
  | { type: 'session_state'; session_id: string; status: string; radio_mode: string }
  | { type: 'error'; message: string }
```

- [ ] **Step 2: 创建 client.ts**

```ts
// API 客户端基础封装

const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function apiClient<T>(path: string, options: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${BASE_URL}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}))
    throw new ApiError(resp.status, body.detail || `请求失败: ${resp.status}`)
  }
  return resp.json() as Promise<T>
}

export { BASE_URL }
```

- [ ] **Step 3: 创建 sessions.ts**

```ts
import { apiClient } from './client'
import type { Session, Transcript, Answer } from './types'

export function createSession(title: string): Promise<Session> {
  return apiClient<Session>('/api/sessions', {
    method: 'POST',
    body: JSON.stringify({ title }),
  })
}

export function listSessions(): Promise<Session[]> {
  return apiClient<Session[]>('/api/sessions')
}

export function getSession(id: string): Promise<Session> {
  return apiClient<Session>(`/api/sessions/${id}`)
}

export function endSession(id: string): Promise<Session> {
  return apiClient<Session>(`/api/sessions/${id}/end`, { method: 'POST' })
}

export function getTranscripts(id: string): Promise<Transcript[]> {
  return apiClient<Transcript[]>(`/api/sessions/${id}/transcripts`)
}

export function getAnswers(id: string): Promise<Answer[]> {
  return apiClient<Answer[]>(`/api/sessions/${id}/answers`)
}

export function generateReview(id: string): Promise<{ session_id: string; review: string }> {
  return apiClient<{ session_id: string; review: string }>(`/api/sessions/${id}/review`, {
    method: 'POST',
  })
}
```

- [ ] **Step 4: 创建 configs.ts**

```ts
import { apiClient } from './client'
import type { Config } from './types'

export function getConfigs(type: string): Promise<Config[]> {
  return apiClient<Config[]>(`/api/configs/${type}`)
}

export function saveConfig(type: string, name: string, data: Record<string, string>, isActive: boolean): Promise<Config> {
  return apiClient<Config>(`/api/configs/${type}`, {
    method: 'POST',
    body: JSON.stringify({ name, data, is_active: isActive }),
  })
}

export function activateConfig(type: string, id: number): Promise<Config> {
  return apiClient<Config>(`/api/configs/${type}/activate/${id}`, { method: 'POST' })
}
```

- [ ] **Step 5: 提交**

```bash
git add desktop/src/api/
git commit -m "feat: API client wrapper for backend REST endpoints"
```

---

### Task 3: WebSocket 客户端

**Files:**
- Create: `desktop/src/api/ws.ts`

**Interfaces:**
- Consumes: `BASE_URL`（来自 client.ts）
- Produces:
  - `connectWS(sessionId: string, onMessage: (msg: WSMessage) => void): WebSocket`
  - `sendAudioChunk(ws: WebSocket, source: string, data: string)`
  - `setRadioMode(ws: WebSocket, mode: string)`
  - `regenerateAnswer(ws: WebSocket, question: string)`
  - `endSessionWS(ws: WebSocket)`

- [ ] **Step 1: 创建 ws.ts**

```ts
import { BASE_URL } from './client'
import type { WSMessage } from './types'

// 将 http(s) 转为 ws(s)
function toWS(url: string): string {
  return url.replace(/^http/, 'ws')
}

export function connectWS(sessionId: string, onMessage: (msg: WSMessage) => void): WebSocket {
  const ws = new WebSocket(`${toWS(BASE_URL)}/ws/${sessionId}`)
  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data) as WSMessage
      onMessage(msg)
    } catch {
      // 忽略无法解析的消息
    }
  }
  return ws
}

export function sendAudioChunk(ws: WebSocket, source: string, data: string): void {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'audio_chunk', source, data }))
  }
}

export function setRadioMode(ws: WebSocket, mode: string): void {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'set_radio_mode', mode }))
  }
}

export function regenerateAnswer(ws: WebSocket, question: string): void {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'regenerate_answer', question }))
  }
}

export function endSessionWS(ws: WebSocket): void {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'end_session' }))
  }
}
```

- [ ] **Step 2: 提交**

```bash
git add desktop/src/api/ws.ts
git commit -m "feat: WebSocket client for realtime transcription"
```

---

### Task 4: 麦克风收音 Hook

**Files:**
- Create: `desktop/src/hooks/useRecorder.ts`

**Interfaces:**
- Consumes: `sendAudioChunk`（来自 ws.ts）
- Produces:
  - `useRecorder(onChunk: (base64: string) => void)` → `{ start, stop, isRecording, error }`

- [ ] **Step 1: 创建 useRecorder.ts**

```ts
import { useCallback, useRef, useState } from 'react'

interface RecorderState {
  start: () => Promise<void>
  stop: () => void
  isRecording: boolean
  error: string | null
}

export function useRecorder(onChunk: (base64: string) => void): RecorderState {
  const [isRecording, setIsRecording] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)

  const start = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
      const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm' })
      mediaRecorderRef.current = recorder

      // 每 2 秒切分一次音频，转 base64 上传
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          const reader = new FileReader()
          reader.onloadend = () => {
            const base64 = (reader.result as string).split(',')[1]
            onChunk(base64)
          }
          reader.readAsDataURL(event.data)
        }
      }

      recorder.start(2000)
      setIsRecording(true)
      setError(null)
    } catch (e) {
      setError(`无法访问麦克风: ${e}`)
    }
  }, [onChunk])

  const stop = useCallback(() => {
    mediaRecorderRef.current?.stop()
    streamRef.current?.getTracks().forEach((track) => track.stop())
    setIsRecording(false)
  }, [])

  return { start, stop, isRecording, error }
}
```

- [ ] **Step 2: 提交**

```bash
git add desktop/src/hooks/useRecorder.ts
git commit -m "feat: microphone recorder hook with chunked base64 upload"
```

---

### Task 5: 面试页面（实时转写 + 答案）

**Files:**
- Create: `desktop/src/pages/Interview.tsx`

**Interfaces:**
- Consumes: `createSession`, `endSession`, `connectWS`, `sendAudioChunk`, `setRadioMode`, `regenerateAnswer`, `useRecorder`
- Produces: 面试页面组件（转写流 + 答案卡片 + 收音端切换 + 会话控制）

- [ ] **Step 1: 创建 Interview.tsx**

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Card, Radio, Space, Typography, List, Tag, message } from 'antd'
import { AudioOutlined, AudioMutedOutlined, ReloadOutlined } from '@ant-design/icons'
import { createSession, endSession } from '../api/sessions'
import { connectWS, sendAudioChunk, setRadioMode, regenerateAnswer } from '../api/ws'
import { useRecorder } from '../hooks/useRecorder'
import type { WSMessage, Transcript, Answer } from '../api/types'

const { Title, Text, Paragraph } = Typography

function Interview() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [radioMode, setRadioModeState] = useState<'pc' | 'mobile' | 'both'>('pc')
  const [transcripts, setTranscripts] = useState<Transcript[]>([])
  const [answers, setAnswers] = useState<Answer[]>([])
  const [status, setStatus] = useState<'idle' | 'recording' | 'ended'>('idle')
  const wsRef = useRef<WebSocket | null>(null)

  const handleChunk = useCallback((base64: string) => {
    if (wsRef.current && sessionId) {
      sendAudioChunk(wsRef.current, 'pc', base64)
    }
  }, [sessionId])

  const recorder = useRecorder(handleChunk)

  const handleMessage = useCallback((msg: WSMessage) => {
    if (msg.type === 'transcript') {
      setTranscripts((prev) => [...prev, {
        id: Date.now(),
        session_id: msg.session_id,
        source: msg.source,
        text: msg.text,
        timestamp: new Date().toISOString(),
        seq: msg.seq,
      }])
    } else if (msg.type === 'answer') {
      setAnswers((prev) => [...prev, {
        id: Date.now(),
        session_id: msg.session_id,
        question: msg.question,
        answer: msg.answer,
        source: 'llm',
        created_at: new Date().toISOString(),
      }])
    } else if (msg.type === 'session_state') {
      setStatus(msg.status as 'idle' | 'recording' | 'ended')
      setRadioModeState(msg.radio_mode as 'pc' | 'mobile' | 'both')
    } else if (msg.type === 'error') {
      message.error(msg.message)
    }
  }, [])

  const startInterview = useCallback(async () => {
    try {
      const session = await createSession(`面试 ${new Date().toLocaleString()}`)
      setSessionId(session.id)
      wsRef.current = connectWS(session.id, handleMessage)
      await recorder.start()
      setStatus('recording')
    } catch (e) {
      message.error(`开始面试失败: ${e}`)
    }
  }, [handleMessage, recorder])

  const stopInterview = useCallback(async () => {
    recorder.stop()
    if (sessionId) {
      await endSession(sessionId)
    }
    setStatus('ended')
  }, [recorder, sessionId])

  const handleRadioChange = useCallback((mode: 'pc' | 'mobile' | 'both') => {
    setRadioModeState(mode)
    if (wsRef.current) {
      setRadioMode(wsRef.current, mode)
    }
  }, [])

  const handleRegenerate = useCallback((question: string) => {
    if (wsRef.current) {
      regenerateAnswer(wsRef.current, question)
    }
  }, [])

  useEffect(() => {
    return () => {
      recorder.stop()
      wsRef.current?.close()
    }
  }, [recorder])

  return (
    <div className="max-w-4xl mx-auto">
      <Title level={3}>实时面试</Title>

      <Card className="mb-4">
        <Space direction="vertical" size="middle" className="w-full">
          <Space>
            <Text>收音端：</Text>
            <Radio.Group
              value={radioMode}
              onChange={(e) => handleRadioChange(e.target.value)}
              disabled={status === 'ended'}
            >
              <Radio.Button value="pc">电脑</Radio.Button>
              <Radio.Button value="mobile">手机</Radio.Button>
              <Radio.Button value="both">双端</Radio.Button>
            </Radio.Group>
          </Space>

          <Space>
            {status === 'idle' && (
              <Button type="primary" icon={<AudioOutlined />} onClick={startInterview}>
                开始面试
              </Button>
            )}
            {status === 'recording' && (
              <Button danger icon={<AudioMutedOutlined />} onClick={stopInterview}>
                结束面试
              </Button>
            )}
            {status === 'ended' && <Tag color="green">面试已结束</Tag>}
            {recorder.isRecording && <Tag color="red">录音中</Tag>}
          </Space>
        </Space>
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card title="实时转写" className="h-[500px] overflow-y-auto">
          <List
            dataSource={transcripts}
            renderItem={(item) => (
              <List.Item>
                <div className="w-full">
                  <Space className="mb-1">
                    <Tag color={item.source === 'pc' ? 'blue' : 'green'}>
                      {item.source === 'pc' ? '电脑' : '手机'}
                    </Tag>
                    <Text type="secondary" className="text-xs">{item.seq}</Text>
                  </Space>
                  <Paragraph className="mb-0">{item.text}</Paragraph>
                </div>
              </List.Item>
            )}
          />
        </Card>

        <Card title="AI 答案" className="h-[500px] overflow-y-auto">
          <List
            dataSource={answers}
            renderItem={(item) => (
              <List.Item>
                <div className="w-full">
                  <Text strong className="block mb-1">Q: {item.question}</Text>
                  <Paragraph className="mb-1">{item.answer}</Paragraph>
                  <Button
                    size="small"
                    icon={<ReloadOutlined />}
                    onClick={() => handleRegenerate(item.question)}
                  >
                    重新生成
                  </Button>
                </div>
              </List.Item>
            )}
          />
        </Card>
      </div>
    </div>
  )
}

export default Interview
```

- [ ] **Step 2: 在 App.tsx 中引入面试页面**

```tsx
import Interview from './pages/Interview'
// ...
<Route path="/" element={<Interview />} />
```

- [ ] **Step 3: 启动验证**

```bash
cd desktop
npm run dev
# 浏览器访问 http://localhost:5173
# 点击"开始面试"应能创建会话并开始录音
```

- [ ] **Step 4: 提交**

```bash
git add desktop/src/pages/Interview.tsx desktop/src/App.tsx
git commit -m "feat: interview page with realtime transcription and answers"
```

---

### Task 6: 设置页面（LLM + 搜索配置）

**Files:**
- Create: `desktop/src/pages/Settings.tsx`

**Interfaces:**
- Consumes: `getConfigs`, `saveConfig`, `activateConfig`
- Produces: 设置页面组件（LLM 配置 + 搜索 API 配置，统一一个页面）

- [ ] **Step 1: 创建 Settings.tsx**

```tsx
import { useCallback, useEffect, useState } from 'react'
import { Card, Form, Input, Button, List, Tag, Space, Select, message, Popconfirm } from 'antd'
import { getConfigs, saveConfig, activateConfig } from '../api/configs'
import type { Config } from '../api/types'

const { Title, Text } = Typography

function Settings() {
  const [llmConfigs, setLlmConfigs] = useState<Config[]>([])
  const [searchConfigs, setSearchConfigs] = useState<Config[]>([])
  const [llmForm] = Form.useForm()
  const [searchForm] = Form.useForm()

  const loadConfigs = useCallback(async () => {
    try {
      const [llm, search] = await Promise.all([
        getConfigs('llm'),
        getConfigs('search'),
      ])
      setLlmConfigs(llm)
      setSearchConfigs(search)
    } catch (e) {
      message.error(`加载配置失败: ${e}`)
    }
  }, [])

  useEffect(() => {
    loadConfigs()
  }, [loadConfigs])

  const handleSaveLlm = useCallback(async (values: Record<string, string>) => {
    try {
      await saveConfig('llm', values.name, {
        base_url: values.base_url,
        api_key: values.api_key,
        model: values.model,
        auth_field: values.auth_field || 'Authorization',
      }, true)
      message.success('LLM 配置已保存')
      loadConfigs()
      llmForm.resetFields()
    } catch (e) {
      message.error(`保存失败: ${e}`)
    }
  }, [loadConfigs, llmForm])

  const handleSaveSearch = useCallback(async (values: Record<string, string>) => {
    try {
      await saveConfig('search', values.name, {
        engine: values.engine,
        api_key: values.api_key,
        cx: values.cx || '',
      }, true)
      message.success('搜索配置已保存')
      loadConfigs()
      searchForm.resetFields()
    } catch (e) {
      message.error(`保存失败: ${e}`)
    }
  }, [loadConfigs, searchForm])

  const handleActivate = useCallback(async (type: string, id: number) => {
    try {
      await activateConfig(type, id)
      message.success('已启用')
      loadConfigs()
    } catch (e) {
      message.error(`启用失败: ${e}`)
    }
  }, [loadConfigs])

  return (
    <div className="max-w-4xl mx-auto">
      <Title level={3}>设置</Title>

      <Card title="LLM 配置" className="mb-6">
        <Form
          form={llmForm}
          layout="vertical"
          onFinish={handleSaveLlm}
          className="mb-4"
        >
          <Form.Item name="name" label="配置名称" rules={[{ required: true }]}>
            <Input placeholder="例如：我的中转站" />
          </Form.Item>
          <Form.Item name="base_url" label="请求地址 (Base URL)" rules={[{ required: true }]}>
            <Input placeholder="https://api.example.com/v1" />
          </Form.Item>
          <Form.Item name="api_key" label="API Key" rules={[{ required: true }]}>
            <Input.Password placeholder="sk-..." />
          </Form.Item>
          <Form.Item name="model" label="模型名称" rules={[{ required: true }]}>
            <Input placeholder="gpt-4o / claude-3-5-sonnet" />
          </Form.Item>
          <Form.Item name="auth_field" label="认证字段 (Header 名称)">
            <Input placeholder="Authorization（默认）" />
          </Form.Item>
          <Button type="primary" htmlType="submit">保存并启用</Button>
        </Form>

        <List
          dataSource={llmConfigs}
          renderItem={(item) => (
            <List.Item
              actions={[
                !item.is_active && (
                  <Button size="small" onClick={() => handleActivate('llm', item.id)}>启用</Button>
                ),
              ]}
            >
              <Space>
                <Text strong>{item.name}</Text>
                {item.is_active && <Tag color="green">当前启用</Tag>}
                <Text type="secondary" className="text-xs">{item.data.base_url}</Text>
              </Space>
            </List.Item>
          )}
        />
      </Card>

      <Card title="搜索 API 配置（可选）">
        <Text type="secondary" className="block mb-4">
          未配置搜索时，答案由 LLM 直接生成。配置后可搜索资料增强答案准确性。
        </Text>
        <Form
          form={searchForm}
          layout="vertical"
          onFinish={handleSaveSearch}
          className="mb-4"
        >
          <Form.Item name="name" label="配置名称" rules={[{ required: true }]}>
            <Input placeholder="例如：Google 搜索" />
          </Form.Item>
          <Form.Item name="engine" label="搜索引擎" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'google', label: 'Google Custom Search' },
                { value: 'bing', label: 'Bing Web Search' },
              ]}
            />
          </Form.Item>
          <Form.Item name="api_key" label="API Key" rules={[{ required: true }]}>
            <Input.Password placeholder="搜索 API key" />
          </Form.Item>
          <Form.Item name="cx" label="Google Custom Search Engine ID（仅 Google）">
            <Input placeholder="cx=..." />
          </Form.Item>
          <Button type="primary" htmlType="submit">保存并启用</Button>
        </Form>

        <List
          dataSource={searchConfigs}
          renderItem={(item) => (
            <List.Item
              actions={[
                !item.is_active && (
                  <Button size="small" onClick={() => handleActivate('search', item.id)}>启用</Button>
                ),
              ]}
            >
              <Space>
                <Text strong>{item.name}</Text>
                {item.is_active && <Tag color="green">当前启用</Tag>}
                <Text type="secondary" className="text-xs">{item.data.engine}</Text>
              </Space>
            </List.Item>
          )}
        />
      </Card>
    </div>
  )
}

export default Settings
```

- [ ] **Step 2: 在 App.tsx 中引入设置页面**

```tsx
import Settings from './pages/Settings'
// ...
<Route path="/settings" element={<Settings />} />
```

- [ ] **Step 3: 启动验证**

```bash
cd desktop
npm run dev
# 浏览器访问 http://localhost:5173/settings
# 应能保存 LLM 配置和搜索配置
```

- [ ] **Step 4: 提交**

```bash
git add desktop/src/pages/Settings.tsx desktop/src/App.tsx
git commit -m "feat: settings page for LLM and search API config"
```

---

### Task 7: 历史会话页面

**Files:**
- Create: `desktop/src/pages/History.tsx`

**Interfaces:**
- Consumes: 后端 REST API（会话列表、转写、答案、复盘）
- Produces: 历史会话页面组件（会话列表 + 详情 + 复盘报告）

- [ ] **Step 1: 创建 History.tsx**

```tsx
import { useCallback, useEffect, useState } from 'react'
import { Card, List, Typography, Button, Drawer, Tag, Space, message } from 'antd'
import { listSessions, getTranscripts, getAnswers, generateReview } from '../api/sessions'
import type { Session, Transcript, Answer } from '../api/types'

const { Title, Text, Paragraph } = Typography

function History() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [selected, setSelected] = useState<Session | null>(null)
  const [transcripts, setTranscripts] = useState<Transcript[]>([])
  const [answers, setAnswers] = useState<Answer[]>([])
  const [review, setReview] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const loadSessions = useCallback(async () => {
    try {
      const list = await listSessions()
      setSessions(list)
    } catch (e) {
      message.error(`加载会话失败: ${e}`)
    }
  }, [])

  useEffect(() => {
    loadSessions()
  }, [loadSessions])

  const openSession = useCallback(async (session: Session) => {
    setSelected(session)
    setReview(null)
    try {
      const [ts, ans] = await Promise.all([
        getTranscripts(session.id),
        getAnswers(session.id),
      ])
      setTranscripts(ts)
      setAnswers(ans)
    } catch (e) {
      message.error(`加载会话详情失败: ${e}`)
    }
  }, [])

  const handleReview = useCallback(async () => {
    if (!selected) return
    setLoading(true)
    try {
      const result = await generateReview(selected.id)
      setReview(result.review)
    } catch (e) {
      message.error(`生成复盘失败: ${e}`)
    } finally {
      setLoading(false)
    }
  }, [selected])

  return (
    <div className="max-w-4xl mx-auto">
      <Title level={3}>历史会话</Title>
      <Card>
        <List
          dataSource={sessions}
          locale={{ emptyText: '暂无历史会话' }}
          renderItem={(item) => (
            <List.Item
              actions={[<Button onClick={() => openSession(item)}>查看</Button>]}
            >
              <Space>
                <Text strong>{item.title}</Text>
                <Tag>{item.status}</Tag>
              </Space>
            </List.Item>
          )}
        />
      </Card>

      <Drawer
        title={selected?.title}
        open={!!selected}
        onClose={() => setSelected(null)}
        width={600}
      >
        {selected && (
          <div>
            <Button
              type="primary"
              onClick={handleReview}
              loading={loading}
              className="mb-4"
            >
              生成复盘报告
            </Button>

            {review && (
              <Card title="复盘报告" className="mb-4">
                <Paragraph style={{ whiteSpace: 'pre-wrap' }}>{review}</Paragraph>
              </Card>
            )}

            <Card title="转写记录" className="mb-4">
              <List
                dataSource={transcripts}
                renderItem={(item) => (
                  <List.Item>
                    <div className="w-full">
                      <Tag color={item.source === 'pc' ? 'blue' : 'green'}>
                        {item.source === 'pc' ? '电脑' : '手机'}
                      </Tag>
                      <Paragraph className="mb-0">{item.text}</Paragraph>
                    </div>
                  </List.Item>
                )}
              />
            </Card>

            <Card title="AI 答案">
              <List
                dataSource={answers}
                renderItem={(item) => (
                  <List.Item>
                    <div className="w-full">
                      <Text strong className="block mb-1">Q: {item.question}</Text>
                      <Paragraph className="mb-0">{item.answer}</Paragraph>
                    </div>
                  </List.Item>
                )}
              />
            </Card>
          </div>
        )}
      </Drawer>
    </div>
  )
}

export default History
```

- [ ] **Step 2: 在 App.tsx 中引入历史会话页面**

```tsx
import History from './pages/History'
// ...
<Route path="/history" element={<History />} />
```

- [ ] **Step 3: 提交**

```bash
git add desktop/src/pages/History.tsx desktop/src/App.tsx
git commit -m "feat: history page with session details and review"
```

---

### Task 8: 端到端验证

**Files:**
- Modify: `desktop/README.md`（创建）

**Interfaces:**
- Consumes: 所有已实现模块
- Produces: 可运行的完整 PC 端应用

- [ ] **Step 1: 启动后端**

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- [ ] **Step 2: 启动 PC 端**

```bash
cd desktop
npm run dev
# 浏览器访问 http://localhost:5173
```

- [ ] **Step 3: 手动验证流程**

1. 打开设置页，配置 LLM（base_url、api_key、model）
2. 打开面试页，点击"开始面试"
3. 对着麦克风说话，观察转写是否实时显示
4. 提问（含"吗/如何/什么"等），观察 AI 答案是否生成
5. 切换收音端（电脑/手机/双端），观察状态变化
6. 点击"结束面试"，确认会话结束

- [ ] **Step 4: 创建 desktop/README.md**

```markdown
# PC 端（Electron + React）

AI 面试助手桌面应用。

## 开发

```bash
cd desktop
npm install
cp .env.example .env  # 配置 VITE_API_BASE_URL
npm run dev
```

## 打包

```bash
npm run electron:build
```

## 页面

- `/` — 面试（实时转写 + AI 答案）
- `/history` — 历史会话
- `/settings` — 设置（LLM + 搜索配置）
```

- [ ] **Step 5: 提交**

```bash
git add desktop/
git commit -m "docs: desktop README and end-to-end verification"
```
