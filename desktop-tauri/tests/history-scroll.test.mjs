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

test('历史转写和答案进入页面时直接定位底部，不播放滚动动画', () => {
  for (const source of [transcriptSource, answerSource]) {
    assert.match(source, /useLayoutEffect/)
    assert.match(source, /scrollTop = feed\.scrollHeight/)
    assert.match(source, /followTailRef/)
    assert.doesNotMatch(source, /pb-0/)
    assert.doesNotMatch(source, /scrollIntoView/)
    assert.doesNotMatch(source, /behavior: 'smooth'/)
  }
  assert.match(transcriptSource, /px-4 py-3/)
  assert.match(answerSource, /px-6 py-5/)
  assert.match(detailSource, /overflow-y-auto px-6 py-5/)
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
