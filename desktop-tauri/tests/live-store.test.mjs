import assert from 'node:assert/strict'
import test from 'node:test'
import { installWindowStub, registerTsExtensionResolve } from './helpers.mjs'

installWindowStub()
await registerTsExtensionResolve()

const { applyEngineEvent, isValidSessionStatus, mergeHistory, useLiveStore } = await import(
  '../src/stores/live.ts'
)
const { buildAnswerFeed, pickDisplayVersion } = await import('../src/shared/answer-threads.ts')

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
    captureOn: false,
    pendingQuestions: [],
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
  // H1 delta-only 契约:token 帧只带 delta(answer 恒为空串),done 帧携带全文。
  return {
    kind: 'serverMessage',
    type: 'answer_stream',
    request_id: requestId,
    session_id: sessionId,
    question,
    channel: 'answer',
    delta: done ? '' : text,
    thinking: '',
    answer: done ? text : '',
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

function threadStreamEvent({
  sessionId = 's1',
  requestId,
  threadId = 'th1',
  revision,
  question,
  text,
  done = false,
  started = false,
  failed = false,
  superseded = false
}) {
  // H1 delta-only 契约:token 帧只带 delta(answer 恒为空串),done 帧携带全文。
  return {
    kind: 'serverMessage',
    type: 'answer_stream',
    request_id: requestId,
    thread_id: threadId,
    revision,
    session_id: sessionId,
    question,
    channel: 'answer',
    delta: done ? '' : text,
    thinking: '',
    answer: done ? text : '',
    source: 'llm',
    done,
    started,
    failed,
    superseded
  }
}

test('一卡一答（catch-up swap）：新版开火保留旧段，superseded 冻结旧段', () => {
  // superseded 是对旧后端终止帧的兼容语义；当前后端各 revision 互不取消。
  // 前端 catch-up swap:新版的 started 帧**不删**旧段(它正流着被删会让用户
  // 眼前的答案凭空消失),superseded 帧把旧段冻结成 done+superseded 保留
  // 兜底;渲染层挑「未被取代里答案最长」的一段展示,新版追平长度即接管。
  let state = baseState()
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'req1', revision: 1, question: '浏览器输入 URL 后', text: '', started: true })
    )
  }
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'req1', revision: 1, question: '浏览器输入 URL 后', text: '答案一开头' })
    )
  }
  assert.equal(state.streamingAnswers.req1.answer, '答案一开头')
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'req2', revision: 2, question: '浏览器输入 URL 后发生了什么', text: '', started: true })
    )
  }
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'req2', revision: 2, question: '浏览器输入 URL 后发生了什么', text: '答案二开头' })
    )
  }
  // 两段并存:旧段最长仍是展示版,新版流式追平前不闪断。
  assert.deepEqual(Object.keys(state.streamingAnswers).sort(), ['req1', 'req2'])
  assert.equal(state.streamingAnswers.req2.revision, 2)
  // rev1 的 superseded 终止帧随后到达:冻结(不删、不算失败、答案保留)。
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'req1', revision: 1, question: '浏览器输入 URL 后', text: '答案一开头', done: true, superseded: true })
    )
  }
  assert.ok(state.streamingAnswers.req1, 'superseded 不删段')
  assert.equal(state.streamingAnswers.req1.superseded, true)
  assert.equal(state.streamingAnswers.req1.done, true)
  assert.equal(state.streamingAnswers.req1.failed, false)
  assert.equal(state.streamingAnswers.req1.answer, '答案一开头')
  // rev2 正常收尾;落库答案只是这张卡上的"已入库"标记,流式段保留。
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'req2', revision: 2, question: '浏览器输入 URL 后发生了什么', text: '答案二完整', done: true })
    )
  }
  state = {
    ...state,
    ...applyEngineEvent(state, {
      ...answerEvent(9, 's1', '浏览器输入 URL 后发生了什么', 'req2'),
      thread_id: 'th1'
    })
  }
  assert.equal(state.streamingAnswers.req2.answer, '答案二完整')
  assert.equal(state.answers.length, 1)

  const feed = buildAnswerFeed(Object.values(state.streamingAnswers), state.answers)
  assert.equal(feed.threads.length, 1)
  assert.equal(feed.threads[0].key, 'th1')
  assert.equal(feed.threads[0].question, '浏览器输入 URL 后发生了什么')
  // 两段都在卡内(旧版冻结、新版完成),展示版挑未被取代里最长的。
  assert.equal(feed.threads[0].versions.length, 2)
  assert.equal(feed.threads[0].persisted?.id, 9)
  assert.deepEqual(feed.historyAnswers, [])
  const display = pickDisplayVersion(feed.threads[0].versions)
  assert.equal(display?.request_id, 'req2', 'superseded 段被跳过,新版是展示版')
  assert.equal(display?.answer, '答案二完整')
})

test('superseded 终止帧单独到达也冻结该段（新版 started 帧丢失的兜底）', () => {
  let state = baseState()
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'req1', revision: 1, question: '问', text: '', started: true })
    )
  }
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'req1', revision: 1, question: '问', text: '半截' })
    )
  }
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'req1', revision: 1, question: '问', text: '答案' })
    )
  }
  assert.ok(state.streamingAnswers.req1)
  assert.equal(state.streamingAnswers.req1.answer, '半截答案')
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'req1', revision: 1, question: '问', text: '', done: true, superseded: true })
    )
  }
  // 不删段:半截答案冻结保留,它可能仍是这张卡上最长的可读内容。
  assert.ok(state.streamingAnswers.req1)
  assert.equal(state.streamingAnswers.req1.superseded, true)
  assert.equal(state.streamingAnswers.req1.done, true)
  assert.equal(state.streamingAnswers.req1.answer, '半截答案')
})

test('pickDisplayVersion：未被取代里挑最长，全空时退回最高 revision', () => {
  const mk = (rid, rev, answer, extra = {}) => ({
    request_id: rid,
    revision: rev,
    question: 'q',
    answer,
    done: false,
    failed: false,
    ...extra
  })
  // 旧段更长 → 仍由旧段兜底展示。
  assert.equal(
    pickDisplayVersion([mk('a', 1, '旧版长答案'.repeat(3)), mk('b', 2, '新版短')])?.request_id,
    'a'
  )
  // 新版追平长度 → 自然接管(并列取 revision 高的)。
  assert.equal(
    pickDisplayVersion([mk('a', 1, '等长'), mk('b', 2, '等长')])?.request_id,
    'b'
  )
  // 旧段被取代 → 跳过,即使它更长。
  assert.equal(
    pickDisplayVersion([
      mk('a', 1, '旧版长答案'.repeat(3), { superseded: true }),
      mk('b', 2, '新版')
    ])?.request_id,
    'b'
  )
  // 全部被取代(极端)→ 退回 revision 最高的,显示它的状态而不是空白。
  assert.equal(
    pickDisplayVersion([
      mk('a', 1, 'x', { superseded: true }),
      mk('b', 2, 'y', { superseded: true })
    ])?.request_id,
    'b'
  )
  // 没有任何一段有答案 → revision 最高的(渲染它的生成中/失败态)。
  assert.equal(pickDisplayVersion([mk('a', 1, ''), mk('b', 2, '')])?.request_id, 'b')
  assert.equal(pickDisplayVersion([]), undefined)
})

test('AnswerFeed 每张卡只渲染 pickDisplayVersion 挑出的一段', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(
    new URL('../src/pages/feeds/AnswerFeed.tsx', import.meta.url),
    'utf8'
  )
  assert.match(source, /pickDisplayVersion\(thread\.versions\)/)
  assert.ok(!/>\s*问题：/.test(source), '标题已是累计问题,段内不再重复问题行')
  assert.ok(!source.includes('index > 0 && <hr'), '单版本渲染没有 hr')
  // 常驻重新生成:带 thread_id,答案流回同一张卡;生成中也可点。
  assert.match(source, /api\.live\.regenerate\(thread\.question, false, thread\.key\)/)
})

test('不同问题线程各自一张卡，落库答案挂到自己那张卡上', () => {
  let state = baseState()
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'a1', threadId: 'thA', revision: 1, question: '问题 A', text: 'A1' })
    )
  }
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'b1', threadId: 'thB', revision: 1, question: '问题 B', text: 'B1' })
    )
  }
  state = {
    ...state,
    ...applyEngineEvent(state, { ...answerEvent(10, 's1', '问题 A', 'a1'), thread_id: 'thA' })
  }
  assert.equal(state.streamingAnswers.a1.answer, 'A1')
  assert.equal(state.streamingAnswers.b1.answer, 'B1')

  const feed = buildAnswerFeed(Object.values(state.streamingAnswers), state.answers)
  assert.deepEqual(
    feed.threads.map((t) => t.key).sort(),
    ['thA', 'thB']
  )
  const threadA = feed.threads.find((t) => t.key === 'thA')
  const threadB = feed.threads.find((t) => t.key === 'thB')
  assert.equal(threadA.persisted?.id, 10)
  assert.equal(threadB.persisted, undefined)
  // 已挂到卡片上的落库答案不再出现在历史列表,避免同一段内容渲染两次。
  assert.deepEqual(feed.historyAnswers, [])
})

test('思考过程通道的增量被直接丢弃，不进 store', () => {
  let state = baseState()
  const patch = applyEngineEvent(state, {
    kind: 'serverMessage',
    type: 'answer_stream',
    request_id: 'r-think',
    thread_id: 'th1',
    revision: 1,
    session_id: 's1',
    question: '问题',
    channel: 'thinking',
    delta: '先分析',
    answer: '',
    source: 'llm',
    done: false,
    started: true,
    failed: false
  })
  state = { ...state, ...patch }
  assert.deepEqual(state.streamingAnswers, {})
})

test('buildAnswerFeed 保留未被卡片覆盖的历史答案', () => {
  const answers = [
    { ...answerRow(1, 's1', '历史问题'), request_id: 'old', thread_id: 'thOld' },
    { ...answerRow(2, 's1', '实时问题'), request_id: 'live1', thread_id: 'thLive' }
  ]
  const streaming = [
    {
      request_id: 'live1',
      thread_id: 'thLive',
      revision: 1,
      session_id: 's1',
      question: '实时问题',
      answer: '实时答案',
      source: 'llm',
      done: true,
      started: true,
      failed: false
    }
  ]
  const feed = buildAnswerFeed(streaming, answers)
  assert.deepEqual(
    feed.historyAnswers.map((a) => a.id),
    [1]
  )
  assert.equal(feed.threads.length, 1)
  assert.equal(feed.threads[0].persisted?.id, 2)
})

test('buildAnswerFeed 最新线程在最前、历史答案按 id 倒序', () => {
  // 2026-09-22 用户拍板:答案区最新在最上,新内容从顶部把旧的往下挤,
  // 阅读位置不被流式输出打扰(也因此没有任何自动滚动)。
  const streaming = [
    {
      request_id: 'r1',
      thread_id: 'th1',
      revision: 1,
      session_id: 's1',
      question: '第一问',
      answer: 'A1',
      source: 'llm',
      done: true,
      started: true,
      failed: false
    },
    {
      request_id: 'r2',
      thread_id: 'th2',
      revision: 1,
      session_id: 's1',
      question: '第二问',
      answer: 'A2',
      source: 'llm',
      done: true,
      started: true,
      failed: false
    }
  ]
  const answers = [answerRow(1, 's1'), answerRow(2, 's1'), answerRow(3, 's1')]
  const feed = buildAnswerFeed(streaming, answers)
  assert.deepEqual(
    feed.threads.map((t) => t.key),
    ['th2', 'th1']
  )
  assert.deepEqual(
    feed.historyAnswers.map((a) => a.id),
    [3, 2, 1]
  )
})

test('buildAnswerFeed 按 revision 排序且标题不会被更短的旧问题改回去', () => {
  const mk = (requestId, revision, question, answer) => ({
    request_id: requestId,
    thread_id: 'th1',
    revision,
    session_id: 's1',
    question,
    answer,
    source: 'llm',
    done: true,
    started: true,
    failed: false
  })
  // 乱序进来:revision 3 先到、revision 1 最后到。
  const feed = buildAnswerFeed(
    [
      mk('r3', 3, '很长的累计问题第三版内容', 'A3'),
      mk('r1', 1, '短', 'A1'),
      mk('r2', 2, '中等长度问题', 'A2')
    ],
    []
  )
  assert.equal(feed.threads.length, 1)
  assert.equal(feed.threads[0].question, '很长的累计问题第三版内容')
  assert.deepEqual(
    feed.threads[0].versions.map((v) => v.answer),
    ['A1', 'A2', 'A3']
  )
})

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

test('answer stream replaces cumulative text and survives the persisted answer', () => {
  let state = baseState()
  state = { ...state, ...applyEngineEvent(state, streamEvent('s1', '第一段')) }
  assert.equal(state.streamingAnswers.r1.answer, '第一段')
  state = { ...state, ...applyEngineEvent(state, streamEvent('s1', '第一段第二段', true)) }
  assert.equal(state.streamingAnswers.r1.done, true)
  state = { ...state, ...applyEngineEvent(state, answerEvent(3, 's1', '流式问题', 'r1')) }
  // 实时分段不再被落库答案清掉:用户要求每一版都保留到会话结束。
  assert.equal(state.streamingAnswers.r1.answer, '第一段第二段')
  assert.equal(state.answers.length, 1)
})

test('token 帧按 delta 累计，全文只在 done 帧可信', () => {
  // H1 delta-only 契约:token 帧 answer 恒为空串,只有 delta 是增量;
  // done 帧携带服务端权威全文(catch-up swap 换成全文)。
  let state = baseState()
  state = { ...state, ...applyEngineEvent(state, streamEvent('s1', '先', false, 'r1')) }
  state = { ...state, ...applyEngineEvent(state, streamEvent('s1', '给', false, 'r1')) }
  state = { ...state, ...applyEngineEvent(state, streamEvent('s1', '出方案', false, 'r1')) }
  assert.equal(state.streamingAnswers.r1.answer, '先给出方案')
  assert.equal(state.streamingAnswers.r1.done, false)
  // done 帧的全文是权威值:即使与累计不同也以它为准。
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      { ...streamEvent('s1', '先给出方案（修订全文）', true), request_id: 'r1' }
    )
  }
  assert.equal(state.streamingAnswers.r1.answer, '先给出方案（修订全文）')
  assert.equal(state.streamingAnswers.r1.done, true)
})

test('failed done 帧全文为空时保留已累计内容', () => {
  let state = baseState()
  state = { ...state, ...applyEngineEvent(state, streamEvent('s1', '已经流出的半截', false, 'r1')) }
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      { ...streamEvent('s1', '', true, 'r1'), failed: true }
    )
  }
  assert.equal(state.streamingAnswers.r1.failed, true)
  assert.equal(state.streamingAnswers.r1.done, true)
  assert.equal(state.streamingAnswers.r1.answer, '已经流出的半截')
})

test('REST 历史答案先落地、WS 重放副本被 id 去重后线程卡仍带已入库标记', () => {
  // C1 回归:REST 历史答案必须自带 thread_id;否则它先落进 answers、
  // WS 重放的同 id 副本又被 id 去重丢掉,线程卡永远等不到 persisted 标记。
  let state = baseState()
  state = {
    ...state,
    ...applyEngineEvent(
      state,
      threadStreamEvent({ requestId: 'req1', revision: 1, question: '线程问题', text: '流式回答', done: true })
    )
  }
  state = {
    ...state,
    ...mergeHistory(state, 's1', [], [
      { ...answerRow(9, 's1', '线程问题'), request_id: 'req1', thread_id: 'th1' }
    ])
  }
  // WS 重放副本后到:同 id 被去重,不重复入列。
  state = {
    ...state,
    ...applyEngineEvent(state, { ...answerEvent(9, 's1', '线程问题', 'req1'), thread_id: 'th1' })
  }
  assert.equal(state.answers.length, 1)
  assert.equal(state.answers[0].thread_id, 'th1')

  const feed = buildAnswerFeed(Object.values(state.streamingAnswers), state.answers)
  assert.equal(feed.threads.length, 1)
  assert.equal(feed.threads[0].persisted?.id, 9)
  assert.deepEqual(feed.historyAnswers, [])
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
  assert.equal(state.streamingAnswers.r1.answer, '第一段完整')
  assert.equal(state.streamingAnswers.r2.question, '第二段问题')
})

test('后端仍下发的 thinking 字段被忽略，落库答案只保留正文', () => {
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
      thinking: '先分析',
      answer: '',
      source: 'llm',
      done: false
    })
  }
  // 思考过程功能已下线:thinking 通道整条丢弃,不产生任何分段。
  assert.deepEqual(state.streamingAnswers, {})
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
  assert.equal(state.answers[0].answer, '最终答案')
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

// ---------- 五轮:captureState 跟随 + pending 提问反馈 ----------

test('captureState flips store captureOn and a disconnect clears it', () => {
  // 悬浮窗 Ctrl+Alt+Z 开的采集也走 captureState 事件:主窗口的开始/停止
  // 按钮不能只看本地 systemAudioOn。引擎断开时门必关,不留假"采集中"。
  const on = applyEngineEvent(baseState(), { kind: 'captureState', active: true })
  assert.equal(on.captureOn, true)
  const off = applyEngineEvent(on, { kind: 'captureState', active: false })
  assert.equal(off.captureOn, false)

  const closed = applyEngineEvent(
    { ...baseState(), captureOn: true },
    { kind: 'connection', phase: 'closed', note: '连接已断开' }
  )
  assert.equal(closed.captureOn, false, '引擎断开采集门必关')
})

test('a sent question stays pending until the first stream frame takes over', () => {
  // 用户痛点:发送后没有任何可见反馈,不知道发没发出去只能反复发。发送
  // 成功即挂 pending;answer_stream 首帧(started 空帧,后端生成一开始就发)
  // 同文本即摘除;失败帧同样带 question 也能收尾;断线兜底清空。
  useLiveStore.getState().reset('s1')
  const store = useLiveStore.getState()
  store.addPendingQuestion('讲讲缓存穿透')
  store.addPendingQuestion('讲讲缓存穿透')
  assert.equal(useLiveStore.getState().pendingQuestions.length, 2)

  store.apply({
    kind: 'serverMessage',
    type: 'answer_stream',
    session_id: 's1',
    request_id: 'r1',
    question: '讲讲缓存穿透',
    channel: 'answer',
    delta: '',
    answer: '',
    started: true,
    source: 'llm'
  })
  assert.equal(useLiveStore.getState().pendingQuestions.length, 0, '首帧即摘除同文本 pending')
  assert.ok(useLiveStore.getState().streamingAnswers.r1, '真卡已接管')

  store.addPendingQuestion('Q2')
  store.apply({
    kind: 'serverMessage',
    type: 'answer_stream',
    session_id: 's1',
    request_id: 'r2',
    question: 'Q2',
    channel: 'answer',
    delta: '',
    answer: '',
    failed: true,
    done: true,
    source: 'llm'
  })
  assert.equal(useLiveStore.getState().pendingQuestions.length, 0, '失败帧也要收掉 pending')

  store.addPendingQuestion('Q3')
  store.apply({ kind: 'connection', phase: 'closed' })
  assert.equal(useLiveStore.getState().pendingQuestions.length, 0, '断线兜底清空')

  useLiveStore.getState().reset(null)
})
