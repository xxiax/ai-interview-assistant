import assert from 'node:assert/strict'
import test from 'node:test'
import { installWindowStub, registerTsExtensionResolve } from './helpers.mjs'

installWindowStub()
await registerTsExtensionResolve()

const { applyEngineEvent, isValidSessionStatus, mergeHistory, useLiveStore } = await import(
  '../src/stores/live.ts'
)

function baseState(overrides = {}) {
  return {
    sessionId: 's1',
    phase: 'ready',
    sessionStatus: 'recording',
    radioMode: 'pc',
    transcripts: [],
    partialTranscript: null,
    answers: [],
    streamingAnswers: {},
    lastEventId: 0,
    synced: true,
    outbox: null,
    seqWatermark: 5,
    seqWatermarkReady: true,
    audioFault: null,
    systemAudioFault: null,
    ...overrides
  }
}

function transcriptEvent(id, sessionId, seq, text = `t${id}`) {
  return { kind: 'serverMessage', type: 'transcript', id, session_id: sessionId, source: 'pc', text, timestamp: '2026-08-21T00:00:00Z', seq }
}

function partialTranscriptEvent(sessionId, text, source = 'pc') {
  return { kind: 'serverMessage', type: 'transcript_partial', session_id: sessionId, source, text }
}

function answerEvent(id, sessionId, question = `q${id}`, requestId = `r${id}`) {
  return { kind: 'serverMessage', type: 'answer', id, session_id: sessionId, question, answer: 'a', source: 'llm', created_at: '2026-08-21T00:00:00Z', request_id: requestId }
}

function streamEvent(sessionId, text, done = false, requestId = 'r1', question = '流式问题') {
  return {
    kind: 'serverMessage',
    type: 'answer_stream',
    request_id: requestId,
    session_id: sessionId,
    question,
    channel: 'answer',
    delta: text,
    text,
    thinking: '',
    answer: text,
    source: 'llm',
    done
  }
}

function transcriptRow(id, sessionId, seq, text = `t${id}`) {
  return { id, session_id: sessionId, source: 'pc', text, timestamp: '2026-08-21T00:00:00Z', seq }
}

function answerRow(id, sessionId, question = `q${id}`) {
  return { id, session_id: sessionId, question, answer: `a${id}`, source: 'llm', created_at: '2026-08-21T00:00:00Z' }
}

test('transcript events dedupe by id', () => {
  let state = baseState()
  const event = transcriptEvent(1, 's1', 1)
  state = { ...state, ...applyEngineEvent(state, event) }
  const again = applyEngineEvent(state, event)
  // 重复事件返回原 state(无补丁)
  assert.equal(again, state)
  assert.equal(state.transcripts.length, 1)
})

test('partial transcript is shown in memory and cleared by the matching final transcript', () => {
  let state = baseState()
  state = { ...state, ...applyEngineEvent(state, partialTranscriptEvent('s1', '请介绍一下')) }
  assert.equal(state.partialTranscript.text, '请介绍一下')
  assert.equal(state.transcripts.length, 0)
  state = { ...state, ...applyEngineEvent(state, transcriptEvent(1, 's1', 1, '请介绍一下你的项目')) }
  assert.equal(state.partialTranscript, null)
  assert.equal(state.transcripts.length, 1)
})

test('transcripts stay sorted by seq', () => {
  let state = baseState()
  for (const [id, seq] of [[2, 2], [1, 1], [3, 3]]) {
    state = { ...state, ...applyEngineEvent(state, transcriptEvent(id, 's1', seq)) }
  }
  assert.deepEqual(
    state.transcripts.map((t) => t.seq),
    [1, 2, 3]
  )
})

test('cross-session transcript/answer events are filtered out', () => {
  let state = baseState({ sessionId: 'new-session' })
  // 旧会话的迟到事件不得落入新会话
  state = { ...state, ...applyEngineEvent(state, transcriptEvent(1, 'old-session', 1)) }
  assert.equal(state.transcripts.length, 0)
  state = { ...state, ...applyEngineEvent(state, answerEvent(1, 'old-session')) }
  assert.equal(state.answers.length, 0)
  // 本会话事件正常进入
  state = { ...state, ...applyEngineEvent(state, transcriptEvent(2, 'new-session', 1)) }
  assert.equal(state.transcripts.length, 1)
  state = { ...state, ...applyEngineEvent(state, answerEvent(2, 'new-session')) }
  assert.equal(state.answers.length, 1)
})

test('answer stream replaces cumulative text and final answer clears the stream', () => {
  let state = baseState()
  state = { ...state, ...applyEngineEvent(state, streamEvent('s1', '第一段')) }
  assert.equal(state.streamingAnswers.r1.answer, '第一段')
  state = { ...state, ...applyEngineEvent(state, streamEvent('s1', '第一段第二段', true)) }
  assert.equal(state.streamingAnswers.r1.done, true)
  state = { ...state, ...applyEngineEvent(state, answerEvent(3, 's1', '流式问题', 'r1')) }
  assert.deepEqual(state.streamingAnswers, {})
})

test('concurrent answer streams remain isolated by request_id', () => {
  let state = baseState()
  state = { ...state, ...applyEngineEvent(state, streamEvent('s1', '第一段输出', false, 'r1', '第一段问题')) }
  state = { ...state, ...applyEngineEvent(state, streamEvent('s1', '第二段输出', false, 'r2', '第二段问题')) }
  state = { ...state, ...applyEngineEvent(state, streamEvent('s1', '第一段完整', true, 'r1', '第一段问题')) }
  assert.equal(state.streamingAnswers.r1.question, '第一段问题')
  assert.equal(state.streamingAnswers.r1.answer, '第一段完整')
  assert.equal(state.streamingAnswers.r2.question, '第二段问题')
  assert.equal(state.streamingAnswers.r2.answer, '第二段输出')

  state = { ...state, ...applyEngineEvent(state, answerEvent(1, 's1', '第一段问题', 'r1')) }
  assert.equal(state.streamingAnswers.r1, undefined)
  assert.equal(state.streamingAnswers.r2.question, '第二段问题')
})

test('final answer keeps upstream thinking content after stream cleanup', () => {
  let state = baseState()
  state = {
    ...state,
    ...applyEngineEvent(state, {
      kind: 'serverMessage',
      type: 'answer_stream',
      request_id: 'r4',
      session_id: 's1',
      question: '流式问题',
      channel: 'thinking',
      delta: '先分析',
      text: '先分析',
      thinking: '先分析',
      answer: '',
      source: 'llm',
      done: false
    })
  }
  state = {
    ...state,
    ...applyEngineEvent(state, {
      kind: 'serverMessage',
      type: 'answer',
      id: 4,
      session_id: 's1',
      question: '流式问题',
      answer: '最终答案',
      thinking: '先分析',
      source: 'llm',
      created_at: '2026-08-21T00:00:00Z',
      request_id: 'r4'
    })
  }
  assert.equal(state.streamingAnswers.r4, undefined)
  assert.equal(state.answers[0].thinking, '先分析')
})

test('audio processing errors keep the backend FunASR network reason', () => {
  const state = baseState()
  const event = {
    kind: 'serverError',
    code: 'audio_processing_failed',
    message: 'FunASR 连接失败，请检查网络或代理配置'
  }
  const next = { ...state, ...applyEngineEvent(state, event) }
  assert.equal(next.audioFault.message, event.message)
})

test('audio processing toast does not replace the backend reason', () => {
  useLiveStore.getState().reset('s1')
  useLiveStore.getState().applyGlobal({
    kind: 'serverError',
    code: 'audio_processing_failed',
    message: 'FunASR 连接失败，请检查网络或代理配置'
  })
  const toasts = useLiveStore.getState().toasts
  assert.equal(toasts.at(-1)?.message, 'FunASR 连接失败，请检查网络或代理配置；已停止采集并取消待处理音频')
})

test('sessionState with invalid status is ignored', () => {
  const state = baseState({ sessionStatus: 'recording' })
  const patch = applyEngineEvent(state, { kind: 'sessionState', status: 'bogus', radioMode: 'pc' })
  assert.equal(patch, state)
})

test('sessionState with valid status is applied', () => {
  const state = baseState({ sessionStatus: 'recording' })
  const patch = applyEngineEvent(state, { kind: 'sessionState', status: 'ended', radioMode: 'both' })
  assert.equal(patch.sessionStatus, 'ended')
  assert.equal(patch.radioMode, 'both')
})

test('isValidSessionStatus runtime guard', () => {
  assert.equal(isValidSessionStatus('idle'), true)
  assert.equal(isValidSessionStatus('recording'), true)
  assert.equal(isValidSessionStatus('ended'), true)
  assert.equal(isValidSessionStatus(''), false)
  assert.equal(isValidSessionStatus(undefined), false)
  assert.equal(isValidSessionStatus(42), false)
})

test('seqWatermark event sets watermark and ready flag', () => {
  const state = baseState({ seqWatermark: 0, seqWatermarkReady: false })
  const patch = applyEngineEvent(state, { kind: 'seqWatermark', nextChunkSeq: 12 })
  assert.equal(patch.seqWatermark, 12)
  assert.equal(patch.seqWatermarkReady, true)
})

test('syncComplete advances lastEventId monotonically', () => {
  const state = baseState({ lastEventId: 10 })
  assert.equal(applyEngineEvent(state, { kind: 'syncComplete', latestEventId: 7 }).lastEventId, 10)
  assert.equal(applyEngineEvent(state, { kind: 'syncComplete', latestEventId: 15 }).lastEventId, 15)
})

test('connection closed with 会话已结束 marks session ended', () => {
  const state = baseState({ sessionStatus: 'recording' })
  const patch = applyEngineEvent(state, { kind: 'connection', phase: 'closed', note: '会话已结束' })
  assert.equal(patch.sessionStatus, 'ended')
  const patch2 = applyEngineEvent(state, { kind: 'connection', phase: 'closed', note: '网络错误' })
  assert.equal(patch2.sessionStatus, 'recording')
})

test('store load failure path: sessions store catches and toasts (no unhandled rejection)', async () => {
  const events = []
  const listener = (e) => events.push(e.detail)
  window.addEventListener('app-toast', listener)
  // Node 环境无 Tauri IPC,api.sessions.list 必然 reject;
  // 若 store.load 未 catch,这里会变成 unhandled rejection 导致进程级失败
  const { useSessionsStore } = await import('../src/stores/sessions.ts')
  await useSessionsStore.getState().load(1, 0)
  assert.equal(useSessionsStore.getState().loading, false)
  window.removeEventListener('app-toast', listener)
  assert.ok(
    events.some((d) => d.kind === 'error' && d.message.includes('加载会话列表失败')),
    'should emit error toast'
  )
})

test('settings store load failure path catches and toasts', async () => {
  const events = []
  const listener = (e) => events.push(e.detail)
  window.addEventListener('app-toast', listener)
  const { useSettingsStore } = await import('../src/stores/settings.ts')
  await useSettingsStore.getState().load()
  assert.equal(useSettingsStore.getState().loading, false)
  window.removeEventListener('app-toast', listener)
  assert.ok(
    events.some((d) => d.kind === 'error' && d.message.includes('读取设置失败')),
    'should emit error toast'
  )
})

test('live store reset clears session state', () => {
  useLiveStore.getState().reset('x')
  assert.equal(useLiveStore.getState().sessionId, 'x')
  assert.equal(useLiveStore.getState().phase, 'idle')
  assert.equal(useLiveStore.getState().transcripts.length, 0)
  assert.deepEqual(useLiveStore.getState().toasts, [])
})

// ---------- mergeHistory:LivePage 挂载回填 ----------

test('mergeHistory unions history with realtime data and sorts transcripts by seq', () => {
  // 场景:实时事件先到(seq 2),历史后到(seq 1、3)——并集后按 seq 升序
  const state = baseState({ transcripts: [transcriptRow(20, 's1', 2, 'realtime')] })
  const patch = mergeHistory(state, 's1', [transcriptRow(10, 's1', 1), transcriptRow(30, 's1', 3)], [])
  assert.deepEqual(
    patch.transcripts.map((t) => ({ id: t.id, seq: t.seq })),
    [
      { id: 10, seq: 1 },
      { id: 20, seq: 2 },
      { id: 30, seq: 3 }
    ]
  )
})

test('mergeHistory dedupes by id (history must not duplicate or replace realtime)', () => {
  const state = baseState({
    transcripts: [transcriptRow(1, 's1', 1, 'realtime-latest')],
    answers: [answerRow(1, 's1', 'realtime-q')]
  })
  const patch = mergeHistory(
    state,
    's1',
    [transcriptRow(1, 's1', 1, 'stale-history')],
    [answerRow(1, 's1', 'stale-history-q')]
  )
  // 同 id:实时为权威,历史被忽略
  assert.equal(patch, null)
})

test('mergeHistory dedupes within history payload itself', () => {
  const patch = mergeHistory(
    baseState(),
    's1',
    [transcriptRow(1, 's1', 1), transcriptRow(1, 's1', 1)],
    [answerRow(1, 's1'), answerRow(1, 's1')]
  )
  assert.equal(patch.transcripts.length, 1)
  assert.equal(patch.answers.length, 1)
})

test('mergeHistory ignores cross-session rows', () => {
  const patch = mergeHistory(
    baseState({ sessionId: 's1' }),
    's1',
    [transcriptRow(1, 'other-session', 1), transcriptRow(2, 's1', 2)],
    [answerRow(1, 'other-session'), answerRow(3, 's1')]
  )
  assert.deepEqual(patch.transcripts.map((t) => t.id), [2])
  assert.deepEqual(patch.answers.map((a) => a.id), [3])
})

test('mergeHistory returns null patch when nothing new (no-op rerender)', () => {
  const state = baseState({ transcripts: [transcriptRow(1, 's1', 1)] })
  assert.equal(mergeHistory(state, 's1', [transcriptRow(1, 's1', 1)], []), null)
  assert.equal(mergeHistory(state, 's1', [], []), null)
})

test('mergeHistory returns null for wrong session (stale in-flight request)', () => {
  // 切换会话后,旧会话的迟到历史不得写入
  const state = baseState({ sessionId: 'new-session' })
  assert.equal(mergeHistory(state, 'old-session', [transcriptRow(1, 'old-session', 1)], []), null)
})

test('mergeHistory keeps realtime-first answers appended and history-prepended order stable', () => {
  // 实时已有 answer id=5;历史补 id=1..4 → 1,2,3,4,5(与后端 id 升序一致)
  const state = baseState({ answers: [answerRow(5, 's1')] })
  const patch = mergeHistory(state, 's1', [], [1, 2, 3, 4].map((id) => answerRow(id, 's1')))
  assert.deepEqual(patch.answers.map((a) => a.id), [1, 2, 3, 4, 5])
})

test('store mergeHistory action merges into live state', () => {
  useLiveStore.getState().reset('s1')
  useLiveStore.getState().apply(transcriptEvent(1, 's1', 1))
  useLiveStore.getState().mergeHistory(
    's1',
    [transcriptRow(1, 's1', 1), transcriptRow(2, 's1', 2)],
    [answerRow(7, 's1')]
  )
  const s = useLiveStore.getState()
  assert.deepEqual(s.transcripts.map((t) => t.id), [1, 2])
  assert.deepEqual(s.answers.map((a) => a.id), [7])
})

test('store mergeHistory action is a no-op for a different session', () => {
  useLiveStore.getState().reset('s1')
  useLiveStore.getState().apply(transcriptEvent(1, 's1', 1))
  useLiveStore.getState().mergeHistory('other', [transcriptRow(9, 'other', 9)], [])
  assert.equal(useLiveStore.getState().transcripts.length, 1)
})
