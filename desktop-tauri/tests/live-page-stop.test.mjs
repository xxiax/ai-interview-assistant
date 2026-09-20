import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

const livePageSource = await readFile(new URL('../src/pages/LivePage.tsx', import.meta.url), 'utf8')
const recorderSource = await readFile(new URL('../src/audio/recorder.ts', import.meta.url), 'utf8')

test('stopCapture awaits system audio shutdown before closing the capture gate', async () => {
  // 系统声音由 Rust 线程组帧:先 await stopSystem(),之后再关闭 capture gate。
  const stopCaptureBody = livePageSource.slice(
    livePageSource.indexOf('const stopCapture'),
    livePageSource.indexOf('// 连接 + 订阅')
  )
  const stopAwaitIndex = stopCaptureBody.indexOf('api.audio.stopSystem()')
  assert.ok(stopAwaitIndex > 0, 'stopCapture must stop system audio')
  const gateIndex = stopCaptureBody.indexOf('api.outbox.setCaptureActive(false')
  assert.ok(gateIndex > stopAwaitIndex, 'capture gate must close after system audio stops')
})

test('stopCapture skips setCaptureActive(false) when a newer capture has taken over', async () => {
  // F3:收尾前检查 epoch 未变,变了说明新采集已接管
  const stopCaptureBody = livePageSource.slice(
    livePageSource.indexOf('const stopCapture'),
    livePageSource.indexOf('// 连接 + 订阅')
  )
  assert.match(
    stopCaptureBody,
    /if \(captureEpochRef\.current !== stopEpoch \+ 1\) return/,
    'must guard final setCaptureActive on epoch snapshot'
  )
})

test('MicRecorder.stop returns a completion promise (recorder exposes done signal)', async () => {
  assert.match(recorderSource, /stop\(\): Promise<void>/)
  assert.match(recorderSource, /stoppedPromise/)
})

test('mic track ended listener is attached on start and removed on cleanup', async () => {
  assert.match(recorderSource, /attachTrackEndedListener\(track, this\.callbacks\.onError\)/)
  assert.match(recorderSource, /for \(const detach of this\.detachTrackEnded\.splice\(0\)\) detach\(\)/)
})

test('live page gives the answer workspace more room than the transcript rail', () => {
  assert.match(livePageSource, /w-\[32%\][^\n]*min-w-\[280px\]/)
  assert.match(livePageSource, /className="flex min-w-0 flex-1 flex-col border-l border-stroke"/)
  assert.match(livePageSource, /AI 回答建议/)
})

test('live transcript rail includes an Agent-style manual AI prompt', () => {
  assert.match(livePageSource, /输入问题，按 Enter 发送给 AI/)
  assert.match(livePageSource, /api\.live\.regenerate\(question, false\)/)
  assert.match(livePageSource, /event\.key === 'Enter' && !event\.shiftKey/)
  assert.match(livePageSource, /Shift\+Enter 换行/)
  assert.match(livePageSource, /aria-label="发送问题"/)
  // 提词输入是单行 composer，占位文字跟着正文左对齐；居中占位会把阅读轴线打断。
  assert.match(livePageSource, /text-left[^\n]*placeholder:text-ink-faint/)
  assert.doesNotMatch(livePageSource, /focus-within:border-brand/)
  assert.match(livePageSource, /disabled:bg-surface-hover/)
})

// ---------- 五轮:采集按钮跟随事件 + 手动提问即时反馈 ----------

test('capture button state follows captureState events, including overlay-started capture', async () => {
  // 悬浮窗 Ctrl+Alt+Z 开的采集:本页 systemAudioOn 没翻,但 store 的
  // captureOn(选择器读成 captureStateOn)跟着 captureState 事件走——按钮必须
  // 如实变"停止系统采集",toggle 也不能把门开着的场景误判成"再开一次"。
  assert.match(livePageSource, /const captureOn = systemAudioOn \|\| captureStateOn/)
  assert.match(livePageSource, /if \(systemAudioOn \|\| useLiveStore\.getState\(\)\.captureOn\)/)
})

test('manual question send mounts a pending card immediately', async () => {
  // 发送成功即挂"正在思考"卡,answer_stream 首帧接棒;不再让用户盯着
  // 按钮转圈猜有没有发出去。
  const answerFeedSource = await readFile(
    new URL('../src/pages/feeds/AnswerFeed.tsx', import.meta.url),
    'utf8'
  )
  assert.match(livePageSource, /addPendingQuestion\(question\)/)
  // H2:pending 订阅下沉到 AnswerPanel,LivePage 本体对 token 帧零重渲染
  assert.match(livePageSource, /const pending = useLiveStore\(\(s\) => s\.pendingQuestions\)/)
  assert.match(livePageSource, /pending=\{pending\}/)
  assert.match(answerFeedSource, /已发送 · 正在思考…/)
})
