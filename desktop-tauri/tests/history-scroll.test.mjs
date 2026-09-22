import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { registerTsExtensionResolve } from './helpers.mjs'

await registerTsExtensionResolve()
const { groupFinalTranscripts } = await import('../src/shared/transcript-display.ts')

const transcriptSource = await readFile(
  new URL('../src/pages/feeds/TranscriptFeed.tsx', import.meta.url),
  'utf8'
)
const answerSource = await readFile(
  new URL('../src/pages/feeds/AnswerFeed.tsx', import.meta.url),
  'utf8'
)
const detailSource = await readFile(
  new URL('../src/pages/SessionDetailPage.tsx', import.meta.url),
  'utf8'
)

test('历史转写进入页面时直接定位底部，不播放滚动动画', () => {
  // 答案区已改为最新在最上(2026-09-22),不再贴底——tail 断言只留给转写区。
  assert.match(transcriptSource, /useLayoutEffect/)
  assert.match(transcriptSource, /scrollTop = feed\.scrollHeight/)
  assert.match(transcriptSource, /followTailRef/)
  for (const source of [transcriptSource, answerSource]) {
    assert.doesNotMatch(source, /pb-0/)
    assert.doesNotMatch(source, /scrollIntoView/)
    assert.doesNotMatch(source, /behavior: 'smooth'/)
  }
  assert.match(transcriptSource, /px-4 py-3/)
  assert.match(answerSource, /px-6 py-5/)
  assert.match(detailSource, /overflow-y-auto px-6 py-5/)
})

test('答案区最新在最上且不做任何自动滚动', () => {
  // 2026-09-22 用户拍板:最新在底部时流式输出会把页面一跳一跳地往上弹,
  // 改为最新从顶部插入、旧内容往下挤;阅读位置不被打扰,自动滚动整体作废。
  assert.doesNotMatch(answerSource, /useLayoutEffect/)
  assert.doesNotMatch(answerSource, /followTailRef/)
  assert.doesNotMatch(answerSource, /scrollTop = feed\.scrollHeight/)
  // 渲染顺序:pending → 实时线程 → 历史(源码里依次出现)。
  const pending = answerSource.indexOf('pending.map')
  const threads = answerSource.indexOf('threads.map')
  const answers = answerSource.indexOf('answers.map')
  assert.ok(pending > -1, 'pending 卡渲染块应存在')
  assert.ok(threads > pending, '实时线程卡应排在 pending 之后')
  assert.ok(answers > threads, '历史答案卡应排在实时线程之后')
})

test('相邻 final 只在显示层合并，跨来源或明显停顿保持分段', () => {
  const rows = [
    { id: 1, session_id: 's', source: 'pc', text: '请介绍一下', timestamp: '2026-08-24T10:00:00Z', seq: 1, chunk_seq: 1, captured_at: '2026-08-24T10:00:00Z' },
    { id: 2, session_id: 's', source: 'pc', text: '你的项目', timestamp: '2026-08-24T10:00:03Z', seq: 2, chunk_seq: 2, captured_at: '2026-08-24T10:00:03Z' },
    { id: 3, session_id: 's', source: 'mobile', text: '手机补充', timestamp: '2026-08-24T10:00:04Z', seq: 3, chunk_seq: 1, captured_at: '2026-08-24T10:00:04Z' },
    { id: 4, session_id: 's', source: 'pc', text: '新的问题', timestamp: '2026-08-24T10:00:20Z', seq: 4, chunk_seq: 3, captured_at: '2026-08-24T10:00:20Z' }
  ]

  const groups = groupFinalTranscripts(rows)
  assert.deepEqual(groups.map((group) => group.text), ['请介绍一下你的项目', '手机补充', '新的问题'])
  assert.deepEqual(groups.map((group) => group.ids), [[1, 2], [3], [4]])
})
